import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414


async def submit(env, key="queue-test"):
    return await env.jobs.submit_probe(env.actor, env.repository.id, key)


async def make_due(env, job_id):
    async with env.factory.begin() as session:
        await session.execute(
            text("UPDATE jobs SET available_at = '2000-01-01 UTC' WHERE id = :id"),
            {"id": job_id},
        )


async def expire(env, job_id):
    async with env.factory.begin() as session:
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = '2000-01-01 UTC' WHERE id = :id"),
            {"id": job_id},
        )


def result():
    from devflow.jobs.schemas import ProbeResult

    return ProbeResult(
        github_repository_id=101,
        full_name="team/private",
        private=True,
        default_branch="main",
        checked_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


async def snapshot(env, job_id):
    from devflow.audit.models import AuditLog
    from devflow.jobs.models import Job, RunEvent

    async with env.factory() as session:
        job = await session.get(Job, job_id)
        events = list(
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.job_id == job_id)
                .order_by(RunEvent.sequence)
            )
        )
        audits = list(
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.job_id == job_id)
                .order_by(AuditLog.created_at)
            )
        )
        return job, events, audits


@pytest.mark.asyncio
async def test_two_workers_claim_once(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    leases = await asyncio.gather(queue.claim(uuid4()), queue.claim(uuid4()))
    valid_leases = [lease for lease in leases if lease is not None]
    assert len(valid_leases) == 1
    claimed, events, audits = await snapshot(env, job.id)
    assert claimed.attempt == valid_leases[0].attempt == 1
    assert claimed.lease_generation == valid_leases[0].generation == 1
    assert claimed.lease_expires_at - claimed.updated_at == timedelta(seconds=30)
    assert [e.payload["status"] for e in events] == ["queued", "running"]
    assert {a.action for a in audits} == {"job.submitted", "job.started"}
    assert next(a for a in audits if a.action == "job.started").actor_id is None


@pytest.mark.asyncio
async def test_claim_skips_locked_job(job_env):
    from devflow.jobs.models import Job
    from devflow.jobs.queue import Queue

    env = job_env
    first = await submit(env, "first")
    second = await submit(env, "second")
    async with env.factory.begin() as session:
        await session.execute(select(Job).where(Job.id == first.id).with_for_update())
        async with asyncio.timeout(1):
            lease = await Queue(env.factory).claim(uuid4())
        assert lease.job_id == second.id


@pytest.mark.asyncio
async def test_stale_generation_cannot_complete(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    old_lease = await queue.claim(uuid4())
    await expire(env, job.id)
    assert await queue.recover_expired() == 1
    await make_due(env, job.id)
    new_lease = await queue.claim(uuid4())
    assert new_lease.generation > old_lease.generation
    before, events, audits = await snapshot(env, job.id)
    assert await queue.complete(old_lease, result()) is False
    assert await queue.heartbeat(old_lease) is False
    assert await queue.fail(old_lease, "github_unavailable", True) is False
    after, new_events, new_audits = await snapshot(env, job.id)
    assert after.result == before.result is None
    assert [e.id for e in new_events] == [e.id for e in events]
    assert [a.id for a in new_audits] == [a.id for a in audits]
    assert await queue.complete(new_lease, result()) is True
    assert await queue.complete(new_lease, result()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["owner", "generation", "job_id"])
async def test_each_lease_identity_field_fences_writes(job_env, wrong):
    from devflow.jobs.queue import Queue

    env = job_env
    await submit(env)
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    forged = replace(lease, **{wrong: 999 if wrong == "generation" else uuid4()})
    assert await queue.heartbeat(forged) is False
    assert await queue.complete(forged, result()) is False
    assert await queue.fail(forged, "github_unavailable", True) is False


@pytest.mark.asyncio
async def test_expired_lease_cannot_renew_or_write_before_recovery(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    await expire(env, job.id)
    assert await queue.heartbeat(lease) is False
    assert await queue.complete(lease, result()) is False
    assert await queue.fail(lease, "github_unavailable", True) is False
    state, events, audits = await snapshot(env, job.id)
    assert state.status == "running"
    assert len(events) == len(audits) == 2


@pytest.mark.asyncio
async def test_retry_budget_and_deadline(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    for attempt, backoff in [(1, 10), (2, 30), (3, None)]:
        lease = await queue.claim(uuid4())
        assert lease.attempt == attempt
        assert await queue.fail(lease, "github_unavailable", True)
        state, _, _ = await snapshot(env, job.id)
        if backoff is not None:
            assert state.status == "retry_wait"
            assert state.available_at - state.updated_at == timedelta(seconds=backoff)
            assert await queue.claim(uuid4()) is None
            await make_due(env, job.id)
        else:
            assert state.status == "failed"
            assert state.error_code == "github_unavailable"
            assert state.lease_owner is state.lease_expires_at is None
    assert await queue.claim(uuid4()) is None
    _, events, audits = await snapshot(env, job.id)
    assert [e.sequence for e in events] == list(range(1, 8))
    assert len(audits) == 7


@pytest.mark.asyncio
async def test_rate_limit_uses_later_provider_deadline(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    async with env.factory() as session:
        deadline = await session.scalar(
            select(func.clock_timestamp() + text("interval '1 hour'"))
        )
    assert await queue.fail(lease, "github_rate_limited", True, deadline)
    state, _, _ = await snapshot(env, job.id)
    assert state.available_at == deadline
    assert await queue.claim(uuid4()) is None


@pytest.mark.asyncio
async def test_third_expired_lease_is_terminal(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    for attempt in (1, 2, 3):
        lease = await queue.claim(uuid4())
        assert lease.attempt == attempt
        await expire(env, job.id)
        assert await queue.recover_expired() == 1
        await make_due(env, job.id)
    state, events, audits = await snapshot(env, job.id)
    assert state.status == "failed"
    assert state.error_code == "lease_expired"
    assert events[-1].payload == {
        "status": "failed",
        "attempt": 3,
        "error_code": "lease_expired",
    }
    assert any(
        a.action == "job.failed" and a.reason_code == "lease_expired" for a in audits
    )
    assert await queue.claim(uuid4()) is None
    assert await queue.recover_expired() == 0


@pytest.mark.asyncio
async def test_nonretryable_failure_is_terminal_and_unknown_code_is_safe(job_env):
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    assert await queue.fail(lease, "raw token=must-not-persist", False)
    state, events, audits = await snapshot(env, job.id)
    assert state.status == "failed"
    assert state.error_code == "internal_error"
    assert events[-1].payload["error_code"] == "internal_error"
    assert any(a.reason_code == "internal_error" for a in audits)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,action",
    [
        ("claim", "job.started"),
        ("complete", "job.succeeded"),
        ("fail", "job.retried"),
        ("recover", "job.retried"),
    ],
)
async def test_state_and_event_roll_back_when_required_audit_fails(
    job_env, operation, action
):
    from sqlalchemy.exc import DBAPIError

    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    lease = None if operation == "claim" else await queue.claim(uuid4())
    if operation == "recover":
        await expire(env, job.id)
    before, events, audits = await snapshot(env, job.id)
    async with env.factory.begin() as session:
        # action comes exclusively from the fixed parameter table above.
        await session.execute(
            text(
                "CREATE FUNCTION reject_worker_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF NEW.action = '{action}' THEN RAISE EXCEPTION 'audit unavailable'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_worker_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_worker_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        if operation == "claim":
            await queue.claim(uuid4())
        elif operation == "complete":
            await queue.complete(lease, result())
        elif operation == "fail":
            await queue.fail(lease, "github_unavailable", True)
        else:
            await queue.recover_expired()
    after, new_events, new_audits = await snapshot(env, job.id)
    assert (
        after.status,
        after.attempt,
        after.lease_generation,
        after.last_sequence,
    ) == (before.status, before.attempt, before.lease_generation, before.last_sequence)
    assert [e.id for e in new_events] == [e.id for e in events]
    assert [a.id for a in new_audits] == [a.id for a in audits]


@pytest.mark.asyncio
async def test_worker_audit_does_not_take_creator_fk_lock(job_env):
    from devflow.auth.models import User
    from devflow.jobs.queue import Queue

    env = job_env
    await submit(env)
    async with env.factory.begin() as session:
        await session.execute(
            select(User).where(User.id == env.actor.id).with_for_update()
        )
        # User → Job callers cannot form a cycle with Job → User audit inserts.
        async with asyncio.timeout(1):
            lease = await Queue(env.factory).claim(uuid4())
        assert lease is not None


@pytest.mark.asyncio
async def test_live_heartbeat_uses_independent_session_and_database_clock(job_env):
    from devflow.jobs.models import Job
    from devflow.jobs.queue import Queue

    env = job_env
    job = await submit(env)
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    # An unrelated handler session remains open while heartbeat commits itself.
    async with env.factory.begin() as handler_session:
        await handler_session.execute(select(Job.id).where(Job.id == job.id))
        assert await queue.heartbeat(lease) is True
        async with env.factory() as observer:
            state = await observer.get(Job, job.id)
            assert state.lease_expires_at - state.updated_at == timedelta(seconds=30)
    _, events, audits = await snapshot(env, job.id)
    assert len(events) == len(audits) == 2


@pytest.mark.asyncio
async def test_queue_operational_metrics_contain_only_safe_fields(job_env, caplog):
    import json
    import logging

    from devflow.jobs.queue import Queue

    env = job_env
    await submit(env)
    caplog.set_level(logging.INFO, logger="devflow")
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    assert await queue.heartbeat(lease)
    assert await queue.fail(lease, "github_unavailable", True)
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "devflow"
    ]
    assert any(
        record.get("action") == "queue.claim" and record["queue_wait_seconds"] >= 0
        for record in records
    )
    assert any(
        record.get("action") == "queue.heartbeat" and record["count"] == 1
        for record in records
    )
    assert any(
        record.get("action") == "queue.failure"
        and record["error_code"] == "github_unavailable"
        for record in records
    )
    assert "user-token" not in caplog.text
    assert "team/private" not in caplog.text
    assert "postgresql" not in caplog.text


@pytest.mark.asyncio
async def test_queue_honors_persisted_lower_attempt_budget(job_env):
    from devflow.jobs.queue import Queue
    from devflow.jobs.service import JobService

    env = job_env
    job = await JobService(env.factory, env.policy, max_attempts=1).submit_probe(
        env.actor, env.repository.id, "one-attempt"
    )
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    assert await queue.fail(lease, "github_unavailable", True)
    state, _, _ = await snapshot(env, job.id)
    assert state.attempt == state.max_attempts == 1
    assert state.status == "failed"
    assert await queue.claim(uuid4()) is None
