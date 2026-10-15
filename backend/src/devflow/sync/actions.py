"""Created-window traversal plus explicit reinspection of known runs/attempts."""

from datetime import timedelta

from devflow.github.data import DataEndpoint as E
from devflow.sync.collectors import collection, fact, finish_target, page, read
from devflow.sync.schemas import SyncTarget, SyncWindow


async def enumerate_runs(work, client, token):
    c = work.cursor
    if c.enumeration_done:
        if c.pending_windows:
            return page(
                work,
                active_window=c.pending_windows[-1],
                pending_windows=c.pending_windows[:-1],
                enumeration_done=False,
                page=1,
            )
        if not c.known_complete and c.mode != "backfill":
            targets = list(work.known_targets)
            return page(
                work,
                phase="known",
                pending_targets=targets,
                known_after_id=targets[-1].source_id if targets else c.known_after_id,
                known_complete=work.known_targets_complete,
            )
        return page(work, stage="done", complete=True)
    window = c.active_window or c.window
    response = await read(
        work,
        client,
        token,
        E.RUNS,
        page=c.page,
        filters={"created": f"{window.since.isoformat()}..{window.until.isoformat()}"},
    )
    if response.total_count >= 1000:
        seconds = int((window.until - window.since).total_seconds())
        if seconds > 1:
            middle = window.since + timedelta(seconds=seconds // 2)
            return page(
                work,
                active_window=SyncWindow(since=window.since, until=middle),
                pending_windows=[
                    *c.pending_windows,
                    SyncWindow(since=middle, until=window.until),
                ],
                page=1,
            )
        full = len(c.unresolved_windows) >= 99
        return page(
            work,
            enumeration_done=True,
            gap_reason="source_limit",
            unresolved_windows=[*c.unresolved_windows, window],
            complete=full,
            stage="done" if full else c.stage,
        )
    targets = [
        SyncTarget(kind="ci_run", source_id=p["id"], attempt=p["run_attempt"])
        for p in response.items
    ]
    return page(
        work,
        pending_targets=targets,
        active_window=window,
        enumeration_done=response.next_page is None,
        page=response.next_page or c.page,
    )


async def collect_step(work, client, token):
    c = work.cursor
    if c.target is None:
        return await enumerate_runs(work, client, token)
    if c.stage in {"detail", "attempts"}:
        attempt = c.current_attempt if c.stage == "attempts" else None
        response = await read(
            work,
            client,
            token,
            E.ATTEMPT if attempt else E.RUN,
            target=c.target.source_id,
            **({"attempt": attempt} if attempt else {}),
        )
        p = response.item
        if p["id"] != c.target.source_id or (attempt and p["run_attempt"] != attempt):
            raise ValueError("Run attempt identity changed")
        if c.stage == "detail":
            return page(
                work,
                latest_attempt=max(p["run_attempt"], c.target.attempt or 1),
                current_attempt=1,
                stage="attempts",
                collections=[collection(work, "attempts", reason="not_collected")],
            )
        value = fact(work, "ci_run", p, attempt=attempt, head_sha=p["head_sha"])
        return page(
            work,
            facts=[value],
            run_head=p["head_sha"],
            stage="jobs",
            child_page=1,
            collections=[
                collection(work, "jobs", reason="not_collected", version=str(attempt))
            ],
        )
    response = await read(
        work,
        client,
        token,
        E.JOBS,
        target=c.target.source_id,
        attempt=c.current_attempt,
        page=c.child_page,
    )
    for p in response.items:
        if (
            p["run_id"] != c.target.source_id
            or p["run_attempt"] != c.current_attempt
            or p["head_sha"] != c.run_head
        ):
            raise ValueError("Job does not belong to captured attempt")
    facts = [
        fact(
            work,
            "ci_job",
            p,
            parent_kind="ci_run",
            parent_source_id=c.target.source_id,
            attempt=c.current_attempt,
            head_sha=c.run_head,
        )
        for p in response.items
    ]
    if response.next_page:
        return page(work, facts=facts, child_page=response.next_page)
    collections = [collection(work, "jobs", version=str(c.current_attempt))]
    if c.current_attempt < c.latest_attempt:
        return page(
            work,
            facts=facts,
            collections=collections,
            current_attempt=c.current_attempt + 1,
            stage="attempts",
            child_page=1,
        )
    return finish_target(
        work, facts=facts, collections=[*collections, collection(work, "attempts")]
    )
