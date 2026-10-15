from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from support.repository_env import browser
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.facts.models import Fact
from devflow.health.service import HealthService
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.sync.models import RepositorySync

pytestmark = pytest.mark.asyncio


async def test_health_request_is_durable_idempotent_and_independent(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    path = f"/api/v1/repositories/{repo.id}/health"
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        result = await client.get(path)
        assert result.status_code == 200
        assert result.json()["snapshot"] is None
        assert result.json()["refresh_status"] == "idle"
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": app.state.auth_service.session_csrf(raw),
            "Idempotency-Key": "health-first",
        }
        result = await client.post(path + "/refresh", json={}, headers=headers)
        assert result.status_code == 202
        assert result.json()["refresh_status"] == "queued"
        assert (
            await client.post(path + "/refresh", json={}, headers=headers)
        ).status_code == 202
    async with env.factory() as db:
        jobs = list(
            await db.scalars(select(Job).where(Job.kind == "repository.health"))
        )
        assert len(jobs) == 1
        assert "generation" not in jobs[0].input


async def health_env(env):
    from support.repository_env import clear_initial_sync

    env.repository = await env.service.register(env.actor, 11, 101)
    await clear_initial_sync(env)
    env.health = HealthService(env.factory, env.policy, env.settings)
    env.queue = Queue(env.factory, settings=env.settings)
    await env.health.request(env.actor, env.repository.id, "first")
    return env, await env.queue.claim(uuid4())


async def test_health_freeze_exact_sha_latest_attempt_and_missing_zero(repo_env):
    env, lease = await health_env(repo_env)
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        db.add(
            Fact(
                repository_id=env.repository.id,
                kind="pull",
                source_id=100,
                number=7,
                state="open",
                source_updated_at=now - timedelta(days=8),
                source_url="https://github.com/team/private/pull/7",
                payload={
                    "title": "P",
                    "state": "open",
                    "issue_id": 101,
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                    "draft": True,
                    "merged": False,
                },
            )
        )
        for source_id, attempt, sha, conclusion in [
            (1, 1, "b", "success"),
            (1, 2, "b", "failure"),
            (2, 1, "c", "success"),
            (3, 1, "b", "unknown"),
        ]:
            db.add(
                Fact(
                    repository_id=env.repository.id,
                    kind="ci_run",
                    source_id=source_id,
                    version_key=str(attempt),
                    source_updated_at=now,
                    source_url=f"https://github.com/team/private/actions/runs/{source_id}",
                    payload={
                        "attempt": attempt,
                        "workflow_id": source_id,
                        "event": "push",
                        "branch": "main",
                        "head_sha": sha * 40,
                        "status": "completed",
                        "conclusion": conclusion,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )
    frozen = await env.health.freeze(lease)
    assert await env.health.publish(lease, frozen)
    view = await env.health.latest(env.actor, env.repository.id)
    assert view.snapshot.counts.open_issues is None
    assert view.snapshot.counts.open_pulls == 1
    assert view.snapshot.counts.stale_pulls == 1
    assert view.snapshot.workspace == "unavailable"
    current = [x for x in view.snapshot.manifest.actions if 7 in x.pr_numbers]
    assert {(x.source_id, x.attempt, x.conclusion) for x in current} == {
        (1, 2, "failure"),
        (3, 1, "unknown"),
    }
    assert view.snapshot.counts.actions_categories == {
        "failure": 1,
        "success": 1,
        "unknown": 1,
    }
    assert all(
        w.created_records is None and not w.complete
        for w in view.snapshot.counts.activity
    )


async def test_old_health_publish_cannot_replace_new(repo_env):
    env, old = await health_env(repo_env)
    first = await env.health.freeze(old)
    # Two valid leases model overlapping publications, independently of admission.
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, env.repository.id)
        state.active_health_job_id = None
    await env.health.request(env.actor, env.repository.id, "new")
    new = await env.queue.claim(uuid4())
    second = await env.health.freeze(new)
    assert second.snapshot.generation > first.snapshot.generation
    assert await env.health.publish(new, second)
    assert await env.health.publish(old, first)
    view = await env.health.latest(env.actor, env.repository.id)
    assert view.snapshot.id == second.snapshot.id
    from devflow.health.models import HealthSnapshot

    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(HealthSnapshot)) == 2


async def test_failed_refresh_keeps_last_snapshot_time(repo_env):
    env, lease = await health_env(repo_env)
    frozen = await env.health.freeze(lease)
    assert await env.health.publish(lease, frozen)
    await env.health.request(env.actor, env.repository.id, "again")
    retry = await env.queue.claim(uuid4())
    deadline = datetime.now(UTC) + timedelta(hours=1)
    assert await env.queue.fail(retry, "github_rate_limited", True, deadline)
    view = await env.health.latest(env.actor, env.repository.id)
    assert view.refresh_status == "retry_wait"
    assert view.retry_at == deadline
    assert view.snapshot.generated_at == frozen.snapshot.generated_at
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == retry.job_id)
            .values(available_at=func.clock_timestamp())
        )
    retry = await env.queue.claim(uuid4())
    assert await env.queue.fail(retry, "internal_error", False)
    view = await env.health.latest(env.actor, env.repository.id)
    assert view.refresh_status == "failed"
    assert view.snapshot.generated_at == frozen.snapshot.generated_at


async def test_health_expiry_and_new_demand_are_fenced(repo_env):
    env, lease = await health_env(repo_env)
    frozen = await env.health.freeze(lease)
    await env.health.request(env.actor, env.repository.id, "during-freeze")
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == lease.job_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert await env.queue.recover_expired() == 1
    assert not await env.health.publish(lease, frozen)
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == lease.job_id)
            .values(available_at=func.clock_timestamp())
        )
    new = await env.queue.claim(uuid4())
    fresh = await env.health.freeze(new)
    assert await env.health.publish(new, fresh)
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.health_processed_sequence == 1
        assert state.health_requested_sequence == 2
        assert state.active_health_job_id != new.job_id
        assert state.health_status == "queued"


async def test_health_short_coverage_zero_and_failed_stream(repo_env):
    from devflow.sync.models import SyncCursor

    env, lease = await health_env(repo_env)
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        db.add(
            SyncCursor(
                repository_id=env.repository.id,
                stream="issues",
                status="complete",
                checkpoint={"stream": "issues", "stage": "done"},
                covered_from=now - timedelta(days=8),
                covered_until=now + timedelta(minutes=1),
                last_success_at=now,
            )
        )
        db.add(
            SyncCursor(
                repository_id=env.repository.id,
                stream="pulls",
                status="failed",
                checkpoint={"stream": "pulls"},
                error_code="internal_error",
            )
        )
    frozen = await env.health.freeze(lease)
    assert frozen.snapshot.counts.open_issues == 0
    assert frozen.snapshot.counts.open_pulls is None
    windows = [w for w in frozen.snapshot.counts.activity if w.stream == "issues"]
    assert [(w.days, w.complete, w.created_records) for w in windows] == [
        (7, True, 0),
        (30, False, 0),
    ]
    assert windows[1].actual_start == now - timedelta(days=8)


async def test_health_manifest_caps_and_default_workflow_created_order(repo_env):
    env, lease = await health_env(repo_env)
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        for number in range(1, 103):
            db.add(
                Fact(
                    repository_id=env.repository.id,
                    kind="pull",
                    source_id=number,
                    number=number,
                    state="open",
                    source_url=f"https://github.com/team/private/pull/{number}",
                    payload={
                        "title": "P",
                        "state": "open",
                        "issue_id": number,
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "draft": False,
                        "merged": False,
                    },
                )
            )
        for number in range(1, 203):
            db.add(
                Fact(
                    repository_id=env.repository.id,
                    kind="ci_run",
                    source_id=number,
                    version_key="1",
                    source_updated_at=now,
                    source_url=f"https://github.com/team/private/actions/runs/{number}",
                    payload={
                        "attempt": 1,
                        "workflow_id": number,
                        "event": "push",
                        "branch": "main",
                        "head_sha": "b" * 40,
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )
        # Updated recently, but older creation must not win default workflow #202.
        db.add(
            Fact(
                repository_id=env.repository.id,
                kind="ci_run",
                source_id=999,
                version_key="2",
                source_updated_at=now + timedelta(seconds=1),
                source_url="https://github.com/team/private/actions/runs/999",
                payload={
                    "attempt": 2,
                    "workflow_id": 202,
                    "event": "push",
                    "branch": "main",
                    "head_sha": "c" * 40,
                    "status": "completed",
                    "conclusion": "failure",
                    "created_at": now - timedelta(days=1),
                    "updated_at": now,
                },
            )
        )
    frozen = await env.health.freeze(lease)
    manifest = frozen.snapshot.manifest
    assert frozen.snapshot.counts.open_pulls == 102
    assert [p.number for p in manifest.pulls] == list(range(1, 101))
    assert (
        manifest.pulls_selection.truncated
        and manifest.pulls_selection.total_candidates == 102
    )
    assert len(manifest.actions) == 200
    assert (
        manifest.actions_selection.truncated
        and manifest.actions_selection.total_candidates == 202
    )
    assert 999 not in [r.source_id for r in manifest.actions]
    assert manifest.actions[0].source_id == 202


@pytest.mark.parametrize("fence", ["owner", "generation", "attempt", "authorization"])
async def test_health_publication_full_lease_and_scope(repo_env, fence):
    from dataclasses import replace

    from devflow.repositories.models import Repository

    env, lease = await health_env(repo_env)
    frozen = await env.health.freeze(lease)
    if fence == "authorization":
        async with env.factory.begin() as db:
            await db.execute(
                update(Repository)
                .where(Repository.id == env.repository.id)
                .values(auth_version=Repository.auth_version + 1)
            )
    else:
        lease = replace(
            lease, **{fence: uuid4() if fence == "owner" else getattr(lease, fence) + 1}
        )
    assert not await env.health.publish(lease, frozen)
    async with env.factory() as db:
        assert (
            await db.get(RepositorySync, env.repository.id)
        ).current_health_id is None


async def test_health_publication_audit_failure_rolls_back(repo_env, monkeypatch):
    from devflow.health.models import HealthSnapshot

    env, lease = await health_env(repo_env)
    frozen = await env.health.freeze(lease)

    async def fail(*args):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(Queue, "_transition", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        await env.health.publish(lease, frozen)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(HealthSnapshot)) == 0
        assert (
            await db.get(RepositorySync, env.repository.id)
        ).current_health_id is None
        assert (await db.get(Job, lease.job_id)).status == "running"


async def test_worker_automatically_publishes_health_after_fact_pass(repo_env):
    import asyncio

    from devflow.worker import run_worker

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(env.settings, stop=stop, github=env.github))
    try:
        async with asyncio.timeout(10):
            while True:
                async with env.factory() as db:
                    state = await db.get(RepositorySync, repo.id)
                    failures = list(
                        await db.scalars(select(Job).where(Job.status == "failed"))
                    )
                    assert not failures, [
                        (str(j.id), j.kind, j.error_code) for j in failures
                    ]
                    if state.current_health_id:
                        assert (
                            state.health_processed_sequence
                            == state.health_requested_sequence
                            == 1
                        )
                        assert state.processed_sequence == state.requested_sequence == 1
                        break
                await asyncio.sleep(0.03)
    finally:
        stop.set()
        await asyncio.wait_for(worker, 5)


async def test_unavailable_latest_attempt_cannot_resurrect_old_success(repo_env):
    env, lease = await health_env(repo_env)
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        for attempt in (1, 2):
            db.add(
                Fact(
                    repository_id=env.repository.id,
                    kind="ci_run",
                    source_id=1,
                    version_key=str(attempt),
                    unavailable=attempt == 2,
                    source_url="https://github.com/team/private/actions/runs/1",
                    payload={
                        "attempt": attempt,
                        "workflow_id": 1,
                        "event": "push",
                        "branch": "main",
                        "head_sha": "b" * 40,
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )
    frozen = await env.health.freeze(lease)
    assert frozen.snapshot.manifest.actions == []
    assert frozen.snapshot.counts.actions_categories is None


async def test_concurrent_requests_coalesce_without_fact_generation_dependency(
    repo_env,
):
    import asyncio

    env, lease = await health_env(repo_env)
    await asyncio.gather(
        *(
            env.health.request(env.actor, env.repository.id, key)
            for key in ("second", "third", "third")
        )
    )
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.health_requested_sequence == 3
        state.generation = 99
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "repository.health")
            )
            == 1
        )
    frozen = await env.health.freeze(lease)
    assert frozen.snapshot.generation == 1
    assert frozen.snapshot.manifest.sync_generation == 99
    assert await env.health.publish(lease, frozen)


@pytest.mark.parametrize("revoked", [False, True])
async def test_terminal_health_failure_preserves_newer_accepted_refresh(
    repo_env, revoked
):
    from devflow.repositories.models import Repository

    env, lease = await health_env(repo_env)
    await env.health.request(env.actor, env.repository.id, "after-claim")
    if revoked:
        async with env.factory.begin() as db:
            await db.execute(
                update(Repository)
                .where(Repository.id == env.repository.id)
                .values(available=False, auth_version=Repository.auth_version + 1)
            )
    assert await env.queue.fail(lease, "internal_error", False)
    next_lease = await env.queue.claim(uuid4())
    if revoked:
        assert next_lease is None
        async with env.factory() as db:
            state = await db.get(RepositorySync, env.repository.id)
            assert state.health_processed_sequence == 0
            assert state.health_requested_sequence == 2
        return
    assert next_lease is not None and next_lease.job_id != lease.job_id
    frozen = await env.health.freeze(next_lease)
    assert await env.health.publish(next_lease, frozen)
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.health_requested_sequence == state.health_processed_sequence == 2


async def test_health_refresh_fresh_authorization_and_csrf(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    path = f"/api/v1/repositories/{repo.id}/health"
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        assert (await client.get(path)).status_code == 200
        assert (await client.post(path + "/refresh", json={})).status_code == 403
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": app.state.auth_service.session_csrf(raw),
            "Idempotency-Key": "fresh",
        }
        assert (
            await client.post(path + "/refresh", json={"extra": 1}, headers=headers)
        ).status_code == 422
        env.repositories = {}
        assert (
            await client.post(path + "/refresh", json={}, headers=headers)
        ).status_code == 404
    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "repository.health")
            )
            == 0
        )
