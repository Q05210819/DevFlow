import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.audit.models import AuditLog
from devflow.jobs.handlers import run_probe
from devflow.jobs.models import Job, RunEvent
from devflow.jobs.queue import Queue
from devflow.worker import _execute


async def observations(env, job_id):
    async with env.factory() as session:
        state = (
            await session.execute(
                select(
                    Job.status,
                    Job.attempt,
                    Job.lease_generation,
                    Job.last_sequence,
                    Job.result,
                    Job.error_code,
                ).where(Job.id == job_id)
            )
        ).one()
        events = set(
            await session.scalars(select(RunEvent.id).where(RunEvent.job_id == job_id))
        )
        # Old policy audits have no job_id, so inspect the entire isolated schema.
        audits = set(await session.scalars(select(AuditLog.id)))
        return state, events, audits


async def pending_denial(env, *, phase, status):
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "denial-race")
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    pending, release = asyncio.Event(), asyncio.Event()
    repository_calls = 0

    async def remote_barrier(request):
        nonlocal repository_calls
        if request.url.path.endswith("/repositories"):
            repository_calls += 1
            if repository_calls == (1 if phase == "policy" else 2):
                pending.set()
                await release.wait()
                env.status = status

    env.before_response = remote_barrier

    async def probe(lease):
        return await run_probe(
            lease.job_id, env.factory, env.policy, env.github, env.credentials
        )

    # Delayed denial resumes before a heartbeat can detect the stolen lease.
    task = asyncio.create_task(_execute(queue, lease, probe, heartbeat_seconds=60))
    try:
        async with asyncio.timeout(5):
            await pending.wait()
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return job, queue, lease, release, task


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["policy", "metadata"])
@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("reclaim", [False, True])
async def test_expired_worker_denial_cannot_append_audit(
    job_env, phase, status, reclaim
):
    env = job_env
    job, queue, old, release, task = await pending_denial(
        env, phase=phase, status=status
    )
    try:
        async with env.factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE jobs SET lease_expires_at = '2000-01-01 UTC' WHERE id = :id"
                ),
                {"id": job.id},
            )
        if reclaim:
            assert await queue.recover_expired() == 1
            async with env.factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE jobs SET available_at = '2000-01-01 UTC' WHERE id = :id"
                    ),
                    {"id": job.id},
                )
            replacement = await queue.claim(uuid4())
            assert replacement.generation > old.generation
        before = await observations(env, job.id)
        release.set()
        async with asyncio.timeout(5):
            await task
        assert await observations(env, job.id) == before
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["policy", "metadata"])
@pytest.mark.parametrize("status", [403, 404])
async def test_current_worker_denial_audits_actor_and_job_in_failure_transaction(
    job_env, phase, status
):
    env = job_env
    job, _, _, release, task = await pending_denial(env, phase=phase, status=status)
    try:
        release.set()
        async with asyncio.timeout(5):
            await task
        async with env.factory() as session:
            state = await session.get(Job, job.id)
            denied = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "permission.denied")
                )
            )
            assert state.status == "failed"
            assert len(denied) == 1
            assert denied[0].actor_id == env.actor.id
            assert denied[0].resource_id == env.repository.id
            assert denied[0].job_id == job.id
            assert denied[0].reason_code == "resource_not_found"
            assert state.last_sequence == 3
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_permission_audit_failure_rolls_back_fenced_job_failure(job_env):
    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "denial-rollback")
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    before = await observations(env, job.id)
    async with env.factory.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_denial_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'permission.denied' THEN RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_denial_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_denial_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await queue.fail(lease, "resource_not_found", False)
    assert await observations(env, job.id) == before


@pytest.mark.asyncio
async def test_default_policy_denial_still_audits_and_evicts_cached_success(job_env):
    from devflow.errors import AppError

    env = job_env
    await env.policy.require(env.actor, env.repository.id, fresh=True)
    env.status = 403
    for fresh in (True, False):
        with pytest.raises(AppError):
            await env.policy.require(env.actor, env.repository.id, fresh=fresh)
    async with env.factory() as session:
        audits = list(
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "permission.denied")
            )
        )
        assert len(audits) == 2
        assert all(
            row.actor_id == env.actor.id and row.job_id is None for row in audits
        )


@pytest.mark.asyncio
async def test_denial_failure_waits_for_user_before_locking_job(job_env):
    from devflow.auth.models import User

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "denial-lock-order")
    queue = Queue(env.factory)
    lease = await queue.claim(uuid4())
    task = None
    try:
        async with env.factory.begin() as blocker:
            await blocker.execute(
                select(User).where(User.id == env.actor.id).with_for_update()
            )
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(queue.fail(lease, "resource_not_found", False))
            async with asyncio.timeout(1):
                while True:
                    async with env.factory() as observer:
                        waiting = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"blocker": blocker_pid},
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
            async with env.factory.begin() as observer:
                # The denied attempt waits on User and cannot already hold Job.
                await observer.execute(
                    select(Job.id).where(Job.id == job.id).with_for_update(nowait=True)
                )
        assert await task is True
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
