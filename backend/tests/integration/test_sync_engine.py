import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from support.sync_env import sync_env as sync_env  # noqa: PLC0414

from devflow.facts.models import Fact
from devflow.facts.schemas import FactInput
from devflow.jobs.models import Job
from devflow.sync.models import RepositorySync, SyncCursor, SyncDemand
from devflow.sync.schemas import SyncTarget


async def demand(env, **kwargs):
    from devflow.sync.scheduler import request_sync

    async with env.factory.begin() as db:
        return await request_sync(db, env.repository.id, reason="refresh", **kwargs)


async def start(env):
    await demand(env)
    lease = await env.queue.claim(uuid4())
    return lease, await env.sync.snapshot(lease)


def issue(env, now):
    return FactInput(
        repository_id=env.repository.id,
        kind="issue",
        source_id=501,
        number=1,
        observed_at=now,
        source_url="https://github.com/team/private/issues/1",
        payload={"title": "A", "state": "open"},
    )


async def finish(env, lease):
    for _ in range(3):
        work = await env.sync.snapshot(lease)
        assert await env.sync.commit_page(
            lease,
            work,
            facts=[],
            collections=[],
            checkpoint=work.cursor.model_copy(update={"stage": "done"}),
            complete=True,
        )


@pytest.mark.asyncio
async def test_event_during_pass_survives_completion(sync_env):
    env = sync_env
    lease, _work = await start(env)
    await demand(env, target=SyncTarget(kind="issue", source_id=777, number=7))
    await finish(env, lease)
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.requested_sequence == 2 and state.processed_sequence == 1
        assert state.active_job_id != lease.job_id
        assert (await db.get(Job, state.active_job_id)).status == "queued"
        demands = list(await db.scalars(select(SyncDemand)))
        assert [d.sequence for d in demands] == [2]
        assert demands[0].target["source_id"] == 777


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["expiry", "attempt", "authorization", "generation"])
async def test_expired_lease_cannot_write_facts_or_cursor(sync_env, fence):
    env = sync_env
    lease, work = await start(env)
    async with env.factory.begin() as db:
        if fence == "expiry":
            job = await db.get(Job, lease.job_id)
            job.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        elif fence == "authorization":
            from devflow.repositories.models import Repository

            repo = await db.get(Repository, env.repository.id)
            repo.auth_version += 1
        elif fence == "generation":
            state = await db.get(RepositorySync, env.repository.id)
            state.generation += 1
    if fence == "attempt":
        lease = replace(lease, attempt=lease.attempt + 1)
    assert not await env.sync.commit_page(
        lease,
        work,
        facts=[issue(env, work.observed_at)],
        collections=[],
        checkpoint=work.cursor.model_copy(update={"page": 2}),
        complete=False,
    )
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Fact)) == 0
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        assert cursor.checkpoint["page"] == 1


@pytest.mark.asyncio
async def test_installation_serialization_across_two_queue_instances(sync_env):
    from devflow.github.models import GitHubInstallation
    from devflow.jobs.queue import Queue
    from devflow.repositories.models import Repository
    from devflow.sync.scheduler import request_sync

    env = sync_env
    await demand(env)
    env.settings.github_allowed_installation_ids.append(13)
    async with env.factory.begin() as db:
        db.add(
            GitHubInstallation(
                installation_id=12, available=True, needs_verification=False
            )
        )
        db.add(
            GitHubInstallation(
                installation_id=13, available=True, needs_verification=False
            )
        )
        await db.flush()
        for source, installation in [(102, 11), (103, 12), (104, 12), (105, 13)]:
            repo = Repository(
                github_repository_id=source,
                installation_id=installation,
                full_name=f"team/repo{source}",
                private=True,
                available=True,
                needs_verification=False,
            )
            db.add(repo)
            await db.flush()
            await request_sync(db, repo.id, reason="registration")
    queues = [env.queue, Queue(env.factory, settings=env.settings)]
    leases = await asyncio.gather(*(queues[i % 2].claim(uuid4()) for i in range(4)))
    assert len([lease for lease in leases if lease]) == 2
    async with env.factory() as db:
        installations = list(
            await db.scalars(
                select(Repository.installation_id)
                .join(Job, Job.repository_id == Repository.id)
                .where(Job.status == "running")
            )
        )
        assert sorted(installations) == [11, 12]
        waiting = list(await db.scalars(select(Job).where(Job.status == "queued")))
        assert all(job.attempt == 0 and job.lease_owner is None for job in waiting)


@pytest.mark.asyncio
async def test_fifth_request_continues_without_retry(sync_env):
    from devflow.github.data import DataEndpoint
    from devflow.sync.handlers import CollectedPage, run_sync

    env = sync_env
    lease, _ = await start(env)

    async def collector(work, client, token):
        await client.read(token, work.scope.full_name, DataEndpoint.ISSUES)
        checkpoint = work.cursor.model_copy(
            update={
                "page": work.cursor.page + 1,
                "pending_targets": [SyncTarget(kind="issue", source_id=888, number=8)],
            }
        )
        return CollectedPage([], [], checkpoint, False)

    await run_sync(lease, env.sync, env.data, env.tokens, collector=collector)
    assert len(env.data_calls) == 5
    async with env.factory() as db:
        job = await db.get(Job, lease.job_id)
        state = await db.get(RepositorySync, env.repository.id)
        assert job.status == "succeeded" and job.result["continued"] is True
        assert job.attempt == 1 and job.request_count == 5
        cursor = await db.get(SyncCursor, (env.repository.id, "issues"))
        assert cursor.checkpoint["page"] == 5
        assert cursor.checkpoint["pending_targets"][0]["source_id"] == 888
        assert state.active_job_id != job.id and state.request_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", ["count", "time"])
async def test_round_budget_survives_restart(sync_env, exhausted):
    from devflow.errors import AppError
    from devflow.sync.service import SyncService

    env = sync_env
    lease, work = await start(env)
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, env.repository.id)
        if exhausted == "count":
            state.request_count = 10000
        else:
            state.round_started_at = datetime.now(UTC) - timedelta(hours=24)
    restarted = SyncService(env.factory, env.settings)
    with pytest.raises(AppError, match="budget") as failure:
        await restarted.reserve_request(lease, work)
    assert failure.value.code == "budget_exhausted"
    assert not env.data_calls
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).request_count == 0


@pytest.mark.asyncio
async def test_request_and_job_rollback_with_caller(sync_env):
    from devflow.sync.scheduler import request_sync

    env = sync_env
    with pytest.raises(RuntimeError):
        async with env.factory.begin() as db:
            await request_sync(db, env.repository.id, reason="refresh")
            raise RuntimeError("caller abort")
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Job)) == 0
        assert await db.get(RepositorySync, env.repository.id) is None


@pytest.mark.asyncio
async def test_checkpoint_failure_rolls_back_facts(sync_env):
    from sqlalchemy.exc import DBAPIError

    env = sync_env
    lease, work = await start(env)
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_cursor() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'checkpoint unavailable'; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_cursor BEFORE UPDATE ON sync_cursors FOR EACH ROW EXECUTE FUNCTION reject_cursor()"
            )
        )
    with pytest.raises(DBAPIError):
        await env.sync.commit_page(
            lease,
            work,
            facts=[issue(env, work.observed_at)],
            collections=[],
            checkpoint=work.cursor.model_copy(update={"page": 2}),
            complete=False,
        )
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Fact)) == 0
        assert (await db.get(SyncCursor, (env.repository.id, "issues"))).checkpoint[
            "page"
        ] == 1


@pytest.mark.asyncio
async def test_budget_exhaustion_requires_explicit_resume(sync_env):
    from devflow.sync.scheduler import request_sync

    env = sync_env
    lease, work = await start(env)
    target = SyncTarget(kind="issue", source_id=501, number=1)
    saved = work.cursor.model_copy(update={"page": 9, "pending_targets": [target]})
    await env.sync.commit_page(
        lease, work, facts=[], collections=[], checkpoint=saved, complete=False
    )
    await env.queue.fail(lease, "budget_exhausted", False)
    await demand(env)
    assert await env.queue.claim(uuid4()) is None
    assert await env.scheduler.tick() == 0
    async with env.factory.begin() as db:
        await request_sync(db, env.repository.id, reason="refresh", resume=True)
    fresh = await env.queue.claim(uuid4())
    resumed = await env.sync.snapshot(fresh)
    assert resumed.cursor == saved
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.request_count == 0
        assert state.processed_sequence == 0 and state.requested_sequence == 3


@pytest.mark.asyncio
async def test_revoked_queued_job_terminates_without_http(sync_env):
    from devflow.repositories.models import Repository

    env = sync_env
    await demand(env)
    async with env.factory.begin() as db:
        repository = await db.get(Repository, env.repository.id)
        repository.available = False
        repository.auth_version += 1
    assert await env.queue.claim(uuid4()) is None
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.status == "failed" and state.active_job_id is None
        assert (await db.scalar(select(Job))).error_code == "permission_revoked"


@pytest.mark.asyncio
async def test_unit_budget_reconstructing_snapshot_does_not_reset_count(sync_env):
    from devflow.sync.service import UnitComplete

    env = sync_env
    lease, work = await start(env)
    for _ in range(5):
        await env.sync.reserve_request(lease, work)
        work = await env.sync.snapshot(lease)
    with pytest.raises(UnitComplete):
        await env.sync.reserve_request(lease, work)
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).request_count == 5
        assert (await db.get(RepositorySync, env.repository.id)).request_count == 5


@pytest.mark.asyncio
async def test_insufficient_http_time_continues_without_request(sync_env):
    from devflow.sync.service import UnitComplete

    env = sync_env
    lease, work = await start(env)
    async with env.factory.begin() as db:
        job = await db.get(Job, lease.job_id)
        job.attempt_started_at = datetime.now(UTC) - timedelta(seconds=111)
    with pytest.raises(UnitComplete):
        await env.sync.reserve_request(lease, work)
    assert await env.sync.continue_job(lease, work)
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).request_count == 0


@pytest.mark.asyncio
async def test_child_cleanup_requires_completed_exact_parent_scan(sync_env):
    from devflow.facts.schemas import CollectionInput

    env = sync_env
    lease, work = await start(env)
    ancient = datetime(2000, 1, 1, tzinfo=UTC)
    async with env.factory.begin() as db:
        for source, parent in [(701, 501), (702, 501), (703, 502)]:
            db.add(
                Fact.from_input(
                    FactInput(
                        repository_id=env.repository.id,
                        kind="comment",
                        source_id=source,
                        parent_kind="issue",
                        parent_source_id=parent,
                        observed_at=ancient,
                        source_url="https://github.com/team/private/issues/1",
                        payload={"body": "old"},
                    )
                )
            )
    fenced = work.cursor.model_copy(
        update={
            "target_started_at": work.observed_at,
            "target": SyncTarget(kind="issue", source_id=501, number=1),
            "stage": "comments",
        }
    )
    assert await env.sync.commit_page(
        lease, work, facts=[], collections=[], checkpoint=fenced, complete=False
    )
    work = await env.sync.snapshot(lease)
    partial = CollectionInput(
        repository_id=env.repository.id,
        parent_kind="issue",
        parent_source_id=501,
        collection="comments",
        complete=False,
        reason="partial",
        observed_at=work.observed_at,
    )
    seen = FactInput(
        repository_id=env.repository.id,
        kind="comment",
        source_id=701,
        parent_kind="issue",
        parent_source_id=501,
        observed_at=ancient,
        source_url="https://github.com/team/private/issues/1",
        payload={"body": "seen"},
    )
    assert await env.sync.commit_page(
        lease,
        work,
        facts=[seen],
        collections=[partial],
        checkpoint=fenced,
        complete=False,
    )
    async with env.factory() as db:
        assert not any(f.unavailable for f in await db.scalars(select(Fact)))
    work = await env.sync.snapshot(lease)
    full = partial.model_copy(update={"complete": True, "reason": None})
    assert await env.sync.commit_page(
        lease,
        work,
        facts=[],
        collections=[full],
        checkpoint=fenced.model_copy(update={"target_started_at": None}),
        complete=False,
    )
    async with env.factory() as db:
        rows = {f.source_id: f for f in await db.scalars(select(Fact))}
        assert (
            rows[702].unavailable
            and not rows[701].unavailable
            and not rows[703].unavailable
        )
        assert rows[701].observed_at >= fenced.target_started_at


@pytest.mark.asyncio
async def test_failure_and_recovery_update_sync_state_with_fenced_lease(sync_env):
    env = sync_env
    lease, _ = await start(env)
    assert not await env.queue.fail(
        replace(lease, attempt=2), "github_unavailable", True
    )
    assert await env.queue.fail(lease, "github_unavailable", True)
    for attempt in (2, 3):
        async with env.factory.begin() as db:
            job = await db.get(Job, lease.job_id)
            job.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        lease = await env.queue.claim(uuid4())
        assert lease.attempt == attempt
        async with env.factory.begin() as db:
            job = await db.get(Job, lease.job_id)
            job.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        assert not await env.queue.heartbeat(lease)
        assert await env.queue.recover_expired() == 1
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.status == "failed" and state.active_job_id is None
        assert (
            await db.get(SyncCursor, (env.repository.id, "issues"))
        ).error_code == "lease_expired"


@pytest.mark.asyncio
async def test_scheduler_coalesces_due_work_and_delays_completed_repository(sync_env):
    env = sync_env
    assert await env.scheduler.tick() == 1
    assert await env.scheduler.tick() == 0
    lease = await env.queue.claim(uuid4())
    await finish(env, lease)
    assert await env.scheduler.tick() == 0
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, env.repository.id)
        assert state.last_reconciled_at
        state.next_due_at = datetime(2000, 1, 1, tzinfo=UTC)
    assert await env.scheduler.tick() == 1
    async with env.factory() as db:
        assert (await db.scalar(select(SyncDemand))).reason == "periodic"


@pytest.mark.asyncio
async def test_selected_backfill_and_later_target_remain_separate(sync_env):
    from devflow.sync.scheduler import request_sync
    from devflow.sync.schemas import BackfillRange

    env = sync_env
    lease, _ = await start(env)
    window = BackfillRange(
        since=datetime(2025, 1, 1, tzinfo=UTC),
        until=datetime(2025, 2, 1, tzinfo=UTC),
        streams=["actions"],
    )
    async with env.factory.begin() as db:
        await request_sync(db, env.repository.id, reason="backfill", backfill=window)
        await request_sync(
            db,
            env.repository.id,
            reason="webhook",
            target=SyncTarget(kind="pull", source_id=201, number=2),
        )
    await finish(env, lease)
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    assert work.captured_sequence == 2 and work.cursor.stream == "actions"
    assert work.cursor.mode == "backfill" and work.cursor.window.since == window.since
    assert await env.sync.commit_page(
        lease,
        work,
        facts=[],
        collections=[],
        checkpoint=work.cursor.model_copy(update={"stage": "done"}),
        complete=True,
    )
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    assert work.captured_sequence == 3 and work.cursor.stream == "pulls"
    assert (
        work.cursor.mode == "targeted"
        and work.cursor.pending_targets[0].source_id == 201
    )


@pytest.mark.asyncio
async def test_known_run_hydration_is_bounded_and_uses_latest_attempt_status(sync_env):
    env = sync_env
    ancient = datetime(2000, 1, 1, tzinfo=UTC)
    async with env.factory.begin() as db:
        for source, attempt, status in [
            (n, 1, "in_progress") for n in range(1000, 1101)
        ] + [(900, 1, "in_progress"), (900, 2, "completed")]:
            db.add(
                Fact.from_input(
                    FactInput(
                        repository_id=env.repository.id,
                        kind="ci_run",
                        source_id=source,
                        version_key=str(attempt),
                        source_updated_at=ancient,
                        observed_at=ancient,
                        source_url=f"https://github.com/team/private/actions/runs/{source}",
                        payload={
                            "attempt": attempt,
                            "workflow_id": 1,
                            "event": "push",
                            "head_sha": "a" * 40,
                            "created_at": ancient,
                            "updated_at": ancient,
                            "status": status,
                        },
                    )
                )
            )
    await demand(env, target=SyncTarget(kind="ci_run", source_id=1000))
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    assert len(work.known_targets) == 100 and not work.known_targets_complete
    assert work.known_targets[0].source_id == 1000
    next_cursor = work.cursor.model_copy(
        update={"known_after_id": work.known_targets[-1].source_id}
    )
    assert await env.sync.commit_page(
        lease, work, facts=[], collections=[], checkpoint=next_cursor, complete=False
    )
    work = await env.sync.snapshot(lease)
    assert [t.source_id for t in work.known_targets] == [
        1100
    ] and work.known_targets_complete


@pytest.mark.asyncio
async def test_system_queue_completion_cannot_bypass_coordinated_sync(sync_env):
    from devflow.jobs.schemas import SyncResult

    env = sync_env
    lease, work = await start(env)
    assert not await env.queue.complete(
        lease,
        SyncResult(
            repository_id=env.repository.id, generation=work.generation, continued=False
        ),
    )
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).status == "running"


@pytest.mark.asyncio
async def test_failed_reservation_rolls_back_both_counters(sync_env):
    from sqlalchemy.exc import DBAPIError

    env = sync_env
    lease, work = await start(env)
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_budget() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.request_count > OLD.request_count THEN RAISE EXCEPTION 'budget storage unavailable'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_budget BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION reject_budget()"
            )
        )
    with pytest.raises(DBAPIError):
        await env.sync.reserve_request(lease, work)
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).request_count == 0
        assert (await db.get(RepositorySync, env.repository.id)).request_count == 0


@pytest.mark.asyncio
async def test_disjoint_backfill_does_not_claim_unsampled_gap(sync_env):
    from devflow.sync.scheduler import request_sync
    from devflow.sync.schemas import BackfillRange

    env = sync_env
    lease, _ = await start(env)
    await finish(env, lease)
    async with env.factory() as db:
        before = await db.get(SyncCursor, (env.repository.id, "actions"))
        covered = before.covered_from, before.covered_until
    async with env.factory.begin() as db:
        await request_sync(
            db,
            env.repository.id,
            reason="backfill",
            backfill=BackfillRange(
                since=datetime(2020, 1, 1, tzinfo=UTC),
                until=datetime(2020, 2, 1, tzinfo=UTC),
                streams=["actions"],
            ),
        )
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    await env.sync.commit_page(
        lease,
        work,
        facts=[],
        collections=[],
        checkpoint=work.cursor.model_copy(update={"stage": "done"}),
        complete=True,
    )
    async with env.factory() as db:
        after = await db.get(SyncCursor, (env.repository.id, "actions"))
        assert (after.covered_from, after.covered_until) == covered


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["fail", "recover"])
async def test_failure_paths_wait_for_scope_before_locking_job(sync_env, operation):
    from devflow.github.models import GitHubInstallation

    env = sync_env
    lease, _ = await start(env)
    if operation == "recover":
        async with env.factory.begin() as db:
            job = await db.get(Job, lease.job_id)
            job.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    async with env.factory.begin() as blocker:
        await blocker.execute(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == 11)
            .with_for_update()
        )
        task = asyncio.create_task(
            env.queue.fail(lease, "github_unavailable", True)
            if operation == "fail"
            else env.queue.recover_expired()
        )
        try:
            await asyncio.sleep(0.05)
            async with env.factory.begin() as observer:
                assert (
                    await observer.scalar(
                        select(Job.id)
                        .where(Job.id == lease.job_id)
                        .with_for_update(nowait=True)
                    )
                    == lease.job_id
                )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_sync_settings_reject_invalid_budgets(settings_factory):
    from pydantic import ValidationError

    for setting, value in [
        ("sync_concurrency", 0),
        ("sync_unit_requests", 0),
        ("sync_unit_seconds", 9),
        ("sync_round_requests", -1),
        ("sync_round_seconds", 0),
        ("sync_history_days", 366),
    ]:
        with pytest.raises(ValidationError):
            settings_factory(**{setting: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "continue"])
async def test_success_and_followup_job_roll_back_together(sync_env, operation):
    from sqlalchemy.exc import DBAPIError

    env = sync_env
    await demand(env, target=SyncTarget(kind="issue", source_id=501, number=1))
    lease = await env.queue.claim(uuid4())
    work = await env.sync.snapshot(lease)
    await demand(env, target=SyncTarget(kind="issue", source_id=502, number=2))
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_followup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'job.submitted' THEN RAISE EXCEPTION 'followup unavailable'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_followup BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_followup()"
            )
        )
    with pytest.raises(DBAPIError):
        if operation == "continue":
            await env.sync.continue_job(lease, work)
        else:
            await env.sync.commit_page(
                lease,
                work,
                facts=[issue(env, work.observed_at)],
                collections=[],
                checkpoint=work.cursor.model_copy(update={"stage": "done"}),
                complete=True,
            )
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).status == "running"
        assert await db.scalar(select(func.count()).select_from(Job)) == 1
        assert await db.scalar(select(func.count()).select_from(Fact)) == 0
        assert (await db.get(RepositorySync, env.repository.id)).processed_sequence == 0
        assert await db.scalar(select(func.count()).select_from(SyncDemand)) == 2


@pytest.mark.asyncio
async def test_http_runs_outside_scope_transaction_and_rechecks_authorization(sync_env):
    from devflow.github.data import DataEndpoint
    from devflow.github.models import GitHubInstallation
    from devflow.sync.handlers import CollectedPage, run_sync

    env = sync_env
    lease, _ = await start(env)

    async def collector(work, client, token):
        async with env.factory.begin() as db:
            installation = await db.scalar(
                select(GitHubInstallation)
                .where(GitHubInstallation.installation_id == 11)
                .with_for_update(nowait=True)
            )
            installation.auth_version += 1
        await client.read(token, work.scope.full_name, DataEndpoint.ISSUES)
        return CollectedPage([], [], work.cursor, False)

    await run_sync(lease, env.sync, env.data, env.tokens, collector=collector)
    assert len(env.data_calls) == 1 and env.data_calls[0].method == "POST"
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).request_count == 1


@pytest.mark.asyncio
async def test_repeated_target_demands_coalesce_in_next_round(sync_env):
    env = sync_env
    lease, _ = await start(env)
    target = SyncTarget(kind="issue", source_id=501, number=1)
    for _ in range(3):
        await demand(env, target=target)
    await finish(env, lease)
    work = await env.sync.snapshot(await env.queue.claim(uuid4()))
    assert work.captured_sequence == 4
    assert work.cursor.pending_targets == [target]
