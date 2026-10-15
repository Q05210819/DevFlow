"""PR collection completion waits for a durable final base/head comparison."""

from dataclasses import replace

from devflow.github.data import DataEndpoint as E
from devflow.sync.collectors import collection, fact, finish_target, page, read
from devflow.sync.issues import enumerate_targets
from devflow.sync.schemas import CapturedSHA, PendingCollection, SyncTarget

CHILDREN = {
    "comments": (E.ISSUE_COMMENTS, "comment", "reviews"),
    "reviews": (E.REVIEWS, "review", "review_comments"),
    "review_comments": (E.REVIEW_COMMENTS, "review_comment", "files"),
    "files": (E.PULL_FILES, "pr_file", "verify"),
}


def completed(work, reason=None):
    c = work.cursor
    version = f"{c.captured_sha.base}:{c.captured_sha.head}"
    return [
        collection(
            work,
            p.collection,
            reason=reason or p.reason,
            version=version if p.collection == "files" else "",
        )
        for p in c.pending_collections
    ]


async def collect_step(work, client, token):
    c = work.cursor
    if c.target is None:
        return await enumerate_targets(work, client, token, pulls=True)
    if c.stage == "issue_identity":
        response = await read(work, client, token, E.ISSUE, target=c.target.number)
        if (
            "pull_request" not in response.item
            or response.item["number"] != c.target.number
        ):
            return finish_target(work, gap_reason="version_unavailable")
        return page(work, issue_id=response.item["id"], stage="detail")
    if c.stage in {"detail", "verify"}:
        response = await read(work, client, token, E.PULL, target=c.target.number)
        p = response.item
        if p["number"] != c.target.number or (
            c.target.source_id is not None and p["id"] != c.target.source_id
        ):
            raise ValueError("Pull identity changed")
        captured = CapturedSHA(base=p["base"]["sha"], head=p["head"]["sha"])
        value = fact(work, "pull", p, issue_id=c.issue_id)
        if c.stage == "verify":
            if captured == c.captured_sha:
                return finish_target(work, facts=[value], collections=completed(work))
            collections = completed(work, "version_changed")
            if c.recaptures >= 3:
                return finish_target(
                    work,
                    facts=[value],
                    collections=collections,
                    gap_reason="version_changed",
                )
            key = str(c.target.source_id)
            if key not in c.recapture_counts and len(c.recapture_counts) >= 1000:
                return page(
                    work,
                    facts=[value],
                    collections=collections,
                    complete=True,
                    stage="done",
                    gap_reason="source_limit",
                )
            return page(
                work,
                facts=[value],
                collections=collections,
                stage="detail",
                recaptures=c.recaptures + 1,
                recapture_counts={**c.recapture_counts, key: c.recaptures + 1},
                pending_collections=[],
                child_page=1,
                file_count=0,
                target_started_at=work.observed_at,
            )
        target = SyncTarget(kind="pull", number=c.target.number, source_id=p["id"])
        local = replace(
            work,
            cursor=c.model_copy(update={"target": target, "captured_sha": captured}),
        )
        return page(
            work,
            target=target,
            captured_sha=captured,
            recaptures=c.recapture_counts.get(str(p["id"]), 0),
            stage="comments",
            facts=[value],
            collections=[
                collection(
                    local,
                    name,
                    reason="not_collected",
                    version=f"{captured.base}:{captured.head}"
                    if name == "files"
                    else "",
                )
                for name in CHILDREN
            ],
        )
    endpoint, kind, next_stage = CHILDREN[c.stage]
    response = await read(
        work, client, token, endpoint, target=c.target.number, page=c.child_page
    )
    facts = [
        fact(
            work,
            kind,
            p,
            parent_kind="pull",
            parent_source_id=c.target.source_id,
            parent_number=c.target.number,
            base_sha=c.captured_sha.base,
            head_sha=c.captured_sha.head,
        )
        for p in response.items
    ]
    count = c.file_count + len(facts) if c.stage == "files" else c.file_count
    limit = c.stage == "files" and count >= 3000
    if response.next_page and not limit:
        return page(work, facts=facts, child_page=response.next_page, file_count=count)
    reason = "source_limit" if limit else None
    return page(
        work,
        facts=facts,
        child_page=1,
        file_count=count,
        stage=next_stage,
        pending_collections=[
            *c.pending_collections,
            PendingCollection(collection=c.stage, reason=reason),
        ],
        gap_reason=c.gap_reason or reason,
    )
