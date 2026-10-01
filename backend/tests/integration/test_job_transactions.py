import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.job_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser

from devflow.audit.models import AuditLog
from devflow.audit.service import AuditEntry, write_audit

pytestmark = pytest.mark.asyncio


async def install_failure(factory, table):
    condition = "NEW.action LIKE 'job.%'" if table == "audit_logs" else "true"
    async with factory.begin() as session:
        await session.execute(
            text(
                f"CREATE FUNCTION reject_job_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN RAISE EXCEPTION 'synthetic sink failure'; END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                f"CREATE TRIGGER reject_job_write BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION reject_job_write()"
            )
        )


@pytest.mark.parametrize("table", ["run_events", "audit_logs"])
async def test_state_event_audit_commit_together(job_env, table):
    from devflow.jobs.models import Job, RunEvent

    env = job_env
    await install_failure(env.factory, table)
    with pytest.raises(DBAPIError):
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory() as session:
        created_jobs = await session.scalar(select(func.count()).select_from(Job))
        created_events = await session.scalar(
            select(func.count()).select_from(RunEvent)
        )
        created_audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "job.submitted")
        )
    assert created_jobs == created_events == created_audits == 0


@pytest.mark.parametrize("table", ["run_events", "audit_logs"])
async def test_existing_state_event_and_audit_rollback_together(job_env, table):
    from devflow.jobs.models import Job, RunEvent
    from devflow.jobs.service import append_event

    env = job_env
    submitted = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    await install_failure(env.factory, table)
    with pytest.raises(DBAPIError):
        async with env.factory.begin() as session:
            job = await session.get(Job, submitted.id)
            job.status, job.attempt = "running", 1
            await append_event(session, job, "running")
            await write_audit(
                session,
                AuditEntry(
                    actor_id=env.actor.id,
                    action="job.retried",
                    resource_type="job",
                    resource_id=job.id,
                    job_id=job.id,
                    outcome="success",
                ),
            )
    async with env.factory() as session:
        job = await session.get(Job, submitted.id)
        assert (job.status, job.attempt, job.last_sequence) == ("queued", 0, 1)
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 1


async def test_append_event_uses_database_sequence_under_concurrency(job_env):
    from devflow.jobs.models import Job, RunEvent
    from devflow.jobs.service import append_event

    env = job_env
    submitted = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    barrier = asyncio.Barrier(2)

    async def append():
        async with env.factory.begin() as session:
            job = await session.get(Job, submitted.id)
            await barrier.wait()
            return (await append_event(session, job, "queued")).sequence

    assert sorted(await asyncio.gather(append(), append())) == [2, 3]
    async with env.factory() as session:
        job = await session.get(Job, submitted.id)
        assert job.last_sequence == 3
        sequences = list(
            await session.scalars(
                select(RunEvent.sequence)
                .where(RunEvent.job_id == job.id)
                .order_by(RunEvent.sequence)
            )
        )
        assert sequences == [1, 2, 3]


async def test_append_event_does_not_commit_its_callers_transaction(job_env):
    from devflow.jobs.models import Job, RunEvent
    from devflow.jobs.service import append_event

    env = job_env
    submitted = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory() as session:
        job = await session.get(Job, submitted.id)
        await append_event(session, job, "queued")
        await session.rollback()
    async with env.factory() as session:
        assert (await session.get(Job, submitted.id)).last_sequence == 1
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 1


async def test_http_database_failure_is_safe_503_and_rolls_back(job_env):
    from devflow.jobs.models import Job

    env = job_env
    await install_failure(env.factory, "audit_logs")
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        current = (await client.get("/api/v1/session")).json()
        response = await client.post(
            f"/api/v1/repositories/{env.repository.id}/access-checks",
            headers={
                "Origin": env.settings.public_base_url,
                "X-CSRF-Token": current["csrf_token"],
                "Idempotency-Key": "db-failure",
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        assert "synthetic sink failure" not in response.text
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 0
