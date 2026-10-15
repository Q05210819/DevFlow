"""Independent open and updated-window issue scans."""

from datetime import datetime

from devflow.github.data import DataEndpoint as E
from devflow.sync.collectors import collection, fact, finish_target, page, read
from devflow.sync.schemas import SyncTarget


async def enumerate_targets(work, client, token, *, pulls=False):
    c = work.cursor
    if c.enumeration_done:
        if c.phase == "open" and c.mode != "backfill":
            return page(work, phase="window", page=1, enumeration_done=False)
        return page(work, stage="done", complete=True)
    phase = "window" if c.mode == "backfill" else c.phase
    filters = {
        "state": "open" if phase == "open" else "all",
        "sort": "updated",
        "direction": "desc",
    }
    if pulls and phase == "window" and c.mode != "backfill":
        filters["state"] = "closed"
    if phase == "window" and not pulls:
        filters["since"] = c.window.since.isoformat()
    response = await read(
        work,
        client,
        token,
        E.PULLS if pulls else E.ISSUES,
        page=c.page,
        filters=filters,
    )
    targets = []
    cutoff = False
    for item in response.items:
        updated = datetime.fromisoformat(item["updated_at"])
        if phase == "window":
            if updated < c.window.since:
                cutoff = True
                continue
            if updated > c.window.until:
                continue
        if not pulls and "pull_request" in item:
            continue
        targets.append(
            SyncTarget(
                kind="pull" if pulls else "issue",
                source_id=item["id"],
                number=item["number"],
            )
        )
    return page(
        work,
        pending_targets=targets,
        page=response.next_page or c.page,
        enumeration_done=cutoff or response.next_page is None,
        phase=phase,
    )


async def collect_step(work, client, token):
    c = work.cursor
    if c.target is None:
        return await enumerate_targets(work, client, token)
    if c.stage == "detail":
        response = await read(work, client, token, E.ISSUE, target=c.target.number)
        if "pull_request" in response.item:
            # The pulls stream resolves the independent canonical PR identity.
            return finish_target(work)
        value = fact(work, "issue", response.item)
        if value.source_id != c.target.source_id or value.number != c.target.number:
            raise ValueError("Issue identity changed")
        return page(
            work,
            facts=[value],
            collections=[collection(work, "comments", reason="not_collected")],
            stage="comments",
        )
    response = await read(
        work, client, token, E.ISSUE_COMMENTS, target=c.target.number, page=c.child_page
    )
    facts = [
        fact(
            work,
            "comment",
            p,
            parent_kind="issue",
            parent_source_id=c.target.source_id,
            parent_number=c.target.number,
        )
        for p in response.items
    ]
    if response.next_page:
        return page(work, facts=facts, child_page=response.next_page)
    return finish_target(work, facts=facts, collections=[collection(work, "comments")])
