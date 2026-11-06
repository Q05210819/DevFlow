import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from support.sync_env import claim_fact_sync
from support.sync_env import sync_env as sync_env  # noqa: PLC0414

from devflow.facts.models import Fact, FactCollection
from devflow.jobs.models import Job
from devflow.sync.models import RepositorySync, SyncCursor
from devflow.sync.schemas import BackfillRange, SyncTarget

NOW = datetime.now(UTC).replace(microsecond=0)


def issue(id=501, number=1, **extra):
    return {
        "id": id,
        "number": number,
        "title": "title",
        "state": "open",
        "body": "body",
        "updated_at": NOW.isoformat(),
        **extra,
    }


def pull(head="b", **extra):
    return issue(
        701,
        base={"sha": "a" * 40, "ref": "main", "repo": {"id": 101}},
        head={"sha": head * 40, "ref": "feature", "repo": {"id": 102}},
        draft=False,
        merged=False,
        **extra,
    )


def run(attempt=2, **extra):
    return {
        "id": 801,
        "run_attempt": attempt,
        "workflow_id": 41,
        "event": "push",
        "head_sha": "c" * 40,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "status": "completed",
        "conclusion": "success",
        **extra,
    }


async def request(env, target=None, window=None):
    from devflow.sync.scheduler import request_sync

    async with env.factory.begin() as db:
        await request_sync(
            db,
            env.repository.id,
            reason="backfill" if window else "refresh",
            target=target,
            backfill=window,
        )


async def drive(env, remote, max_units=100):
    from devflow.github.client import GitHubClient
    from devflow.github.data import GitHubDataClient
    from devflow.github.installations import InstallationTokens
    from devflow.sync.handlers import run_sync

    calls = []

    async def transport(req):
        calls.append(req)
        if req.method == "POST":
            return httpx.Response(
                201,
                json={
                    "token": "test-token",
                    "expires_at": (NOW + timedelta(hours=2)).isoformat(),
                    "permissions": json.loads(req.content)["permissions"],
                    "repositories": [{"id": 101}],
                },
            )
        return remote(req)

    from devflow.sync.collectors import collect_step

    async def checked_step(work, client, token):
        before = len(calls)
        try:
            return await collect_step(work, client, token)
        finally:
            assert len(calls) - before <= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = GitHubClient(http, env.settings)
        # Recreate tokens every unit: a restarted process must remain within 5 requests.
        for _ in range(max_units):
            # This collector fixture drives new units immediately, independent
            # of wall-clock rollback. Retry scheduling remains untouched.
            async with env.factory.begin() as db:
                state = await db.get(RepositorySync, env.repository.id)
                if state and state.active_job_id:
                    await db.execute(
                        update(Job)
                        .where(
                            Job.id == state.active_job_id,
                            Job.repository_id == env.repository.id,
                            Job.kind == "repository.sync",
                            Job.status == "queued",
                            Job.attempt == 0,
                            Job.lease_owner.is_(None),
                            Job.lease_expires_at.is_(None),
                        )
                        .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
                    )
            lease = await claim_fact_sync(env.queue, env.factory, env.repository.id)
            if lease is None:
                async with env.factory() as db:
                    state = await db.get(RepositorySync, env.repository.id)
                    job = (
                        await db.get(Job, state.active_job_id)
                        if state.active_job_id
                        else None
                    )
                    now = await db.scalar(select(func.clock_timestamp()))
                    assert job is None, {
                        "status": job.status,
                        "available_at": job.available_at,
                        "lease_expires_at": job.lease_expires_at,
                        "database_time": now,
                        "requests": job.request_count,
                        "generation": state.generation,
                        "captured_sequence": job.input.get("captured_sequence"),
                        "requested_sequence": state.requested_sequence,
                        "processed_sequence": state.processed_sequence,
                    }
                    assert state.status == "completed", state.status
                return
            before = len(calls)
            await run_sync(
                lease,
                env.sync,
                GitHubDataClient(client),
                InstallationTokens(client),
                collector=checked_step,
            )
            async with env.factory() as db:
                job = await db.get(Job, lease.job_id)
                assert job.request_count == len(calls) - before <= 5
    pytest.fail("Collector did not terminate")


@pytest.mark.asyncio
async def test_pr_issue_id_is_not_pr_id(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))

    def remote(req):
        p = req.url.path
        if p.endswith("/pulls/1"):
            return httpx.Response(200, json=pull())
        if p.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={"url": "ignored"}))
        if p.endswith("/issues/1/comments"):
            return httpx.Response(200, json=[{"id": 601, "body": "comment"}])
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        facts = list(await db.scalars(select(Fact)))
        pr = next(f for f in facts if f.kind == "pull")
        comment = next(f for f in facts if f.kind == "comment")
        assert pr.source_id == 701 and pr.payload["issue_id"] == 501
        assert comment.parent_source_id == 701 and comment.parent_kind == "pull"
        assert all(f.kind != "issue" for f in facts)
        assert all(j.request_count <= 5 for j in await db.scalars(select(Job)))


@pytest.mark.asyncio
async def test_force_push_never_publishes_mixed_file_version(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))
    reads = 0

    def remote(req):
        nonlocal reads
        if req.url.path.endswith("/pulls/1"):
            reads += 1
            return httpx.Response(200, json=pull(head="b" if reads == 1 else "c"))
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        if req.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "A.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                        "patch": "+a",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        rows = list(
            await db.scalars(
                select(FactCollection).where(FactCollection.collection == "files")
            )
        )
        assert {(r.version_key, r.complete, r.reason) for r in rows} == {
            ("a" * 40 + ":" + "b" * 40, False, "version_changed"),
            ("a" * 40 + ":" + "c" * 40, True, None),
        }


@pytest.mark.asyncio
async def test_attempt_jobs_do_not_overwrite_old_attempt(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="ci_run", source_id=801))

    def remote(req):
        path = req.url.path
        if path.endswith("/jobs"):
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "jobs": [
                        {
                            "id": 900 + attempt,
                            "run_id": 801,
                            "run_attempt": attempt,
                            "head_sha": "c" * 40,
                            "name": "test",
                            "status": "completed",
                            "conclusion": "failure" if attempt == 1 else "success",
                            "steps": [
                                {
                                    "number": 1,
                                    "name": "build",
                                    "status": "completed",
                                    "conclusion": "skipped",
                                }
                            ],
                        }
                    ],
                },
            )
        attempt = int(path.rsplit("/", 1)[1]) if "/attempts/" in path else 2
        return httpx.Response(200, json=run(attempt))

    await drive(env, remote)
    async with env.factory() as db:
        jobs = list(
            await db.scalars(
                select(Fact).where(Fact.kind == "ci_job").order_by(Fact.source_id)
            )
        )
        assert [(j.payload["attempt"], j.payload["conclusion"]) for j in jobs] == [
            (1, "failure"),
            (2, "success"),
        ]
        assert {
            f.version_key
            for f in await db.scalars(select(Fact).where(Fact.kind == "ci_run"))
        } == {"1", "2"}


@pytest.mark.asyncio
async def test_dense_ci_window_reports_unresolved_gap(sync_env):
    env = sync_env
    await request(
        env,
        window=BackfillRange(
            since=NOW, until=NOW + timedelta(seconds=1), streams=["actions"]
        ),
    )
    await drive(
        env,
        lambda req: httpx.Response(
            200, json={"total_count": 1000, "workflow_runs": []}
        ),
    )
    async with env.factory() as db:
        cursor = await db.get(SyncCursor, (env.repository.id, "actions"))
        state = await db.get(RepositorySync, env.repository.id)
        assert cursor.status == "partial" and cursor.error_code == "source_limit"
        assert cursor.completed_watermark is None and cursor.covered_until is None
        assert state.active_job_id is None and state.processed_sequence == 1


@pytest.mark.asyncio
async def test_incomplete_traversal_never_removes_children(sync_env):
    env = sync_env
    target = SyncTarget(kind="issue", source_id=501, number=1)
    await request(env, target)

    def initial(req):
        return httpx.Response(
            200,
            json=[{"id": 601, "body": "kept"}, {"id": 602, "body": "removed"}]
            if req.url.path.endswith("/comments")
            else issue(),
        )

    await drive(env, initial)
    await request(env, target)

    def partial(req):
        if not req.url.path.endswith("/comments"):
            return httpx.Response(200, json=issue())
        if req.url.params.get("page") == "2":
            return httpx.Response(403, json={"message": "denied"})
        return httpx.Response(
            200,
            json=[{"id": 601, "body": "updated"}],
            headers={
                "link": f'<https://api.github.com{req.url.path}?page=2>; rel="next"'
            },
        )

    await drive(env, partial)
    async with env.factory() as db:
        comments = list(await db.scalars(select(Fact).where(Fact.kind == "comment")))
        assert len(comments) == 2 and all(not f.unavailable for f in comments)
        collection = await db.scalar(select(FactCollection))
        assert not collection.complete
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        assert (
            cursor.status == "permission_missing" and cursor.completed_watermark is None
        )
    # Deterministic stale evidence even when the host/VM wall clock steps backwards.
    async with env.factory.begin() as db:
        await db.execute(
            update(Fact)
            .where(Fact.source_id == 602)
            .values(observed_at=func.clock_timestamp() - text("interval '1 day'"))
        )
    await request(env, target)
    await drive(
        env,
        lambda req: httpx.Response(
            200,
            json=[{"id": 601, "body": "updated"}]
            if req.url.path.endswith("/comments")
            else issue(),
        ),
    )
    async with env.factory() as db:
        missing = await db.scalar(select(Fact).where(Fact.source_id == 602))
        assert missing.unavailable


@pytest.mark.asyncio
async def test_window_failure_does_not_advance_watermark(sync_env):
    from devflow.errors import AppError

    env = sync_env
    window = BackfillRange(since=NOW - timedelta(days=1), until=NOW, streams=["issues"])
    await request(env, window=window)
    await drive(env, lambda req: httpx.Response(200, json=[]))
    async with env.factory() as db:
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        before = (cursor.completed_watermark, cursor.covered_from, cursor.covered_until)
    await request(
        env,
        window=BackfillRange(
            since=NOW, until=NOW + timedelta(hours=1), streams=["issues"]
        ),
    )

    def broken(req):
        return httpx.Response(200, json=[], headers={"link": "invalid next link"})

    with pytest.raises(AppError, match="GitHub"):
        await drive(env, broken)
    async with env.factory() as db:
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        assert (
            cursor.completed_watermark,
            cursor.covered_from,
            cursor.covered_until,
        ) == before
        assert cursor.status != "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_status", [200, 403, 404])
async def test_missing_target_requires_repository_and_stream_proof(
    sync_env, proof_status
):
    env = sync_env
    target = SyncTarget(kind="issue", source_id=501, number=1)
    await request(env, target)
    await drive(
        env,
        lambda req: httpx.Response(
            200, json=[] if req.url.path.endswith("/comments") else issue()
        ),
    )
    await request(env, target)
    paths = []

    def remote(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(404, json={})
        if req.url.path.endswith("/issues"):
            return httpx.Response(proof_status, json=[] if proof_status == 200 else {})
        return httpx.Response(200, json={"id": 101})

    await drive(env, remote)
    async with env.factory() as db:
        value = await db.scalar(select(Fact).where(Fact.kind == "issue"))
        assert value.unavailable == (proof_status == 200)
        assert sum(p.endswith("/issues/1") for p in paths) == (
            2 if proof_status == 200 else 1
        )
        assert len(paths) == (4 if proof_status == 200 else 3)


@pytest.mark.asyncio
async def test_issue_open_and_window_scans_are_independent(sync_env):
    env = sync_env
    await request(env)
    listed = []

    def remote(req):
        p = req.url.path
        if p.endswith("/issues"):
            listed.append(dict(req.url.params))
            values = (
                [issue(501, 1, updated_at=(NOW - timedelta(days=200)).isoformat())]
                if req.url.params["state"] == "open"
                else [issue(502, 2, state="closed"), issue(503, 3, pull_request={})]
            )
            return httpx.Response(200, json=values)
        if p.endswith("/issues/1"):
            return httpx.Response(200, json=issue(501, 1))
        if p.endswith("/issues/2"):
            return httpx.Response(200, json=issue(502, 2, state="closed"))
        if p.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        assert {
            f.source_id
            for f in await db.scalars(select(Fact).where(Fact.kind == "issue"))
        } == {501, 502}
    assert [p["state"] for p in listed] == ["open", "all"]
    assert "since" not in listed[0] and "since" in listed[1]


@pytest.mark.asyncio
async def test_pr_file_limit_and_missing_patch_are_explicit(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))

    def remote(req):
        path = req.url.path
        if path.endswith("/pulls/1"):
            return httpx.Response(200, json=pull())
        if path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        if path.endswith("/files"):
            number = int(req.url.params["page"])
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": f"{number}-{i}.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "changes": 1,
                    }
                    for i in range(100)
                ],
                headers={
                    "link": f'<https://api.github.com{path}?page={number + 1}>; rel="next"'
                }
                if number < 30
                else {},
            )
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        files = list(await db.scalars(select(Fact).where(Fact.kind == "pr_file")))
        assert len(files) == 3000 and all(
            f.payload["patch_status"] == "missing" for f in files
        )
        group = await db.scalar(
            select(FactCollection).where(FactCollection.collection == "files")
        )
        assert not group.complete and group.reason == "source_limit"
        cursor = await db.get(SyncCursor, (env.repository.id, "pulls"))
        assert cursor.status == "partial" and cursor.completed_watermark is None


@pytest.mark.asyncio
async def test_old_unfinished_run_is_reread_and_attempt_hint_is_preserved(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="ci_run", source_id=801))

    def remote(req):
        path = req.url.path
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"total_count": 0, "jobs": []})
        return httpx.Response(
            200,
            json=run(
                1,
                created_at=(NOW - timedelta(days=200)).isoformat(),
                updated_at=(NOW - timedelta(days=100)).isoformat(),
                status="in_progress",
                conclusion=None,
            ),
        )

    await drive(env, remote)
    await request(env)
    attempts = []

    def rerun(req):
        path = req.url.path
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"total_count": 0, "jobs": []})
        if "/attempts/" in path:
            attempt = int(path.rsplit("/", 1)[1])
            attempts.append(attempt)
            return httpx.Response(200, json=run(attempt))
        if path.endswith("/runs/801"):
            return httpx.Response(200, json=run(2))
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        return httpx.Response(200, json=[])

    await drive(env, rerun)
    assert attempts == [1, 2]
    await request(env, SyncTarget(kind="ci_run", source_id=801, attempt=3))
    await drive(env, rerun)
    assert attempts == [1, 2, 1, 2, 3]


@pytest.mark.asyncio
async def test_pr_window_uses_updated_cutoff_without_since(sync_env):
    env = sync_env
    await request(
        env,
        window=BackfillRange(
            since=NOW - timedelta(days=1), until=NOW, streams=["pulls"]
        ),
    )
    calls = []

    def remote(req):
        calls.append(req)
        assert "since" not in req.url.params
        assert req.url.params["sort"] == "updated" and req.url.params["state"] == "all"
        return httpx.Response(
            200,
            json=[pull(updated_at=(NOW - timedelta(days=2)).isoformat())],
            headers={
                "link": f'<https://api.github.com{req.url.path}?page=2>; rel="next"'
            },
        )

    await drive(env, remote)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pr_recaptures_stop_after_three(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", number=1))
    details = 0

    def remote(req):
        nonlocal details
        if req.url.path.endswith("/pulls/1"):
            details += 1
            return httpx.Response(200, json=pull(head=hex(details)[2:]))
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        return httpx.Response(200, json=[])

    await drive(env, remote)
    assert details == 8
    async with env.factory() as db:
        c = await db.get(SyncCursor, (env.repository.id, "pulls"))
        assert c.status == "partial" and c.error_code == "version_changed"
        assert all(not row.complete for row in await db.scalars(select(FactCollection)))


@pytest.mark.asyncio
async def test_missing_pr_issue_record_does_not_remove_readable_pull(sync_env):
    env = sync_env
    target = SyncTarget(kind="pull", source_id=701, number=1)
    await request(env, target)

    def initial(req):
        if req.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json=pull())
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        return httpx.Response(200, json=[])

    await drive(env, initial)
    await request(env, target)

    def missing_issue(req):
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(404, json={})
        if req.url.path.endswith("/pulls/1"):
            return httpx.Response(200, json=pull())
        if req.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": 101})

    await drive(env, missing_issue)
    async with env.factory() as db:
        value = await db.scalar(select(Fact).where(Fact.kind == "pull"))
        assert not value.unavailable
        cursor = await db.get(SyncCursor, (env.repository.id, "pulls"))
        assert cursor.status == "partial"


@pytest.mark.asyncio
async def test_duplicate_pr_hint_shares_round_recapture_budget(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", number=1))
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))
    # Capture both demands in one round, as if coalesced while queued.
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, env.repository.id)
        job = await db.get(Job, state.active_job_id)
        job.input = {**job.input, "captured_sequence": 2}
    details = 0

    def remote(req):
        nonlocal details
        if req.url.path.endswith("/pulls/1"):
            details += 1
            return httpx.Response(200, json=pull(head=f"{details:040x}"[-1]))
        if req.url.path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        return httpx.Response(200, json=[])

    await drive(env, remote)
    assert details == 10


@pytest.mark.asyncio
async def test_recapture_ledger_capacity_reports_gap_without_dropping_entries(sync_env):
    from dataclasses import replace

    from devflow.github.data import DataResponse
    from devflow.sync.pulls import collect_step
    from devflow.sync.schemas import CapturedSHA, PendingCollection

    env = sync_env
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    ledger = {str(source): 1 for source in range(1000, 2000)}
    work = replace(
        work,
        cursor=work.cursor.model_copy(
            update={
                "target": SyncTarget(kind="pull", source_id=701, number=1),
                "stage": "verify",
                "issue_id": 501,
                "captured_sha": CapturedSHA(base="a" * 40, head="b" * 40),
                "recapture_counts": ledger,
                "pending_collections": [PendingCollection(collection="files")],
            }
        ),
    )

    class Client:
        async def read(self, *args, **kwargs):
            return DataResponse(item=pull(head="c"))

    result = await collect_step(work, Client(), None)
    assert (
        result.complete
        and result.outcome == "partial"
        and result.reason == "source_limit"
    )
    assert result.checkpoint.recapture_counts == ledger
    assert result.collections[0].reason == "version_changed"


@pytest.mark.asyncio
async def test_open_to_closed_pr_revisit_keeps_recapture_budget(sync_env):
    env = sync_env
    await request(env)
    details, phase = 0, "open"

    def remote(req):
        nonlocal details, phase
        path = req.url.path
        if path.endswith("/pulls"):
            phase = req.url.params["state"]
            return httpx.Response(
                200,
                json=[
                    pull(state=phase, updated_at=(NOW - timedelta(days=1)).isoformat())
                ],
            )
        if path.endswith("/pulls/1"):
            details += 1
            return httpx.Response(200, json=pull(head=f"{details:x}", state=phase))
        if path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        return httpx.Response(200, json=[])

    await drive(env, remote)
    assert details == 10  # Eight reads to exhaust three recaptures, two on revisit.
    async with env.factory() as db:
        value = await db.scalar(select(Fact).where(Fact.kind == "pull"))
        assert value.payload["state"] == "closed"
        cursor = await db.get(SyncCursor, (env.repository.id, "pulls"))
        assert cursor.checkpoint["recapture_counts"] == {"701": 3}
        assert cursor.status == "partial" and cursor.error_code == "version_changed"


@pytest.mark.asyncio
async def test_final_pr_verification_can_be_fifth_request(sync_env):
    env = sync_env
    await request(env, SyncTarget(kind="pull", source_id=701, number=1))
    reads = 0

    def remote(req):
        nonlocal reads
        path = req.url.path
        if path.endswith("/pulls/1"):
            reads += 1
            return httpx.Response(200, json=pull())
        if path.endswith("/issues/1"):
            return httpx.Response(200, json=issue(pull_request={}))
        if path.endswith("/issues/1/comments") and req.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"id": 601, "body": "first"}],
                headers={"link": f'<https://api.github.com{path}?page=2>; rel="next"'},
            )
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        jobs = list(
            await db.scalars(
                select(Job)
                .where(Job.kind == "repository.sync")
                .order_by(Job.created_at)
            )
        )
        assert [j.request_count for j in jobs] == [5, 5]
        assert all(j.status == "succeeded" for j in jobs)
        assert reads == 2
        assert all(c.complete for c in await db.scalars(select(FactCollection)))


@pytest.mark.asyncio
async def test_whole_round_exhaustion_keeps_unconsumed_demand(sync_env):
    from devflow.errors import AppError
    from devflow.sync.models import SyncDemand

    env = sync_env
    env.sync.settings = env.settings.model_copy(update={"sync_round_requests": 2})
    await request(env, SyncTarget(kind="issue", source_id=501, number=1))
    with pytest.raises(AppError) as error:
        await drive(env, lambda req: httpx.Response(200, json=issue()))
    assert error.value.code == "budget_exhausted"
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        assert state.processed_sequence == 0 and state.request_count == 2
        assert await db.get(SyncDemand, (env.repository.id, 1)) is not None
        assert (
            cursor.checkpoint["stage"] == "comments"
            and cursor.completed_watermark is None
        )


@pytest.mark.asyncio
async def test_dense_ci_windows_split_across_restarts(sync_env):
    env = sync_env
    await request(
        env,
        window=BackfillRange(
            since=NOW, until=NOW + timedelta(seconds=4), streams=["actions"]
        ),
    )
    windows = []

    def remote(req):
        windows.append(req.url.params["created"])
        return httpx.Response(200, json={"total_count": 1000, "workflow_runs": []})

    await drive(env, remote)
    async with env.factory() as db:
        cursor = await db.get(SyncCursor, (env.repository.id, "actions"))
        assert cursor.status == "partial"
        assert len(cursor.checkpoint["unresolved_windows"]) == 4
        assert cursor.checkpoint["pending_windows"] == []
    assert len(windows) == 7


@pytest.mark.asyncio
async def test_dense_gap_capacity_preserves_unvisited_windows(sync_env):
    from dataclasses import replace

    from devflow.github.data import DataResponse
    from devflow.sync.actions import collect_step
    from devflow.sync.schemas import SyncWindow

    env = sync_env
    await request(
        env,
        window=BackfillRange(
            since=NOW, until=NOW + timedelta(seconds=2), streams=["actions"]
        ),
    )
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    gap = SyncWindow(since=NOW, until=NOW + timedelta(seconds=1))
    remaining = SyncWindow(since=gap.until, until=NOW + timedelta(seconds=2))
    work = replace(
        work,
        cursor=work.cursor.model_copy(
            update={
                "active_window": gap,
                "unresolved_windows": [gap] * 99,
                "pending_windows": [remaining],
            }
        ),
    )

    class Client:
        async def read(self, *args, **kwargs):
            return DataResponse(items=[], total_count=1000)

    result = await collect_step(work, Client(), None)
    assert (
        result.complete
        and result.outcome == "partial"
        and result.reason == "source_limit"
    )
    assert len(result.checkpoint.unresolved_windows) == 100
    assert result.checkpoint.pending_windows == [remaining]


@pytest.mark.asyncio
async def test_permission_missing_only_finishes_affected_stream(sync_env):
    env = sync_env
    await request(env)

    def remote(req):
        if req.url.path.endswith("/issues"):
            return httpx.Response(403, json={})
        if req.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        return httpx.Response(200, json=[])

    await drive(env, remote)
    async with env.factory() as db:
        cursors = {c.stream: c for c in await db.scalars(select(SyncCursor))}
        assert cursors["issues"].status == "permission_missing"
        assert cursors["issues"].completed_watermark is None
        assert all(cursors[s].status == "complete" for s in ("pulls", "actions"))
        state = await db.get(RepositorySync, env.repository.id)
        assert state.processed_sequence == 1 and state.active_job_id is None
