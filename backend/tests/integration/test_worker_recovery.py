import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414


async def wait_until(predicate, *, timeout=15):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.05)


def start_worker(env, tmp_path, name, mode):
    config = tmp_path / f"{name}.json"
    values = env.settings.model_dump(mode="json")
    values["database_url"] = env.settings.database_url.get_secret_value()
    config.write_text(json.dumps(values))
    marker, stop = tmp_path / f"{name}.ready", tmp_path / f"{name}.stop"
    script = Path(__file__).parents[1] / "support" / "worker_process.py"
    child = subprocess.Popen(
        [sys.executable, str(script), str(config), str(marker), str(stop), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return child, marker, stop


async def reap(child):
    if child.poll() is None:
        child.kill()  # Only this test's owned Popen PID.
    return await asyncio.to_thread(child.communicate, timeout=10)


@pytest.mark.asyncio
async def test_killed_worker_is_recovered_by_new_process_without_api(job_env, tmp_path):
    from devflow.audit.models import AuditLog
    from devflow.jobs.models import Job, RunEvent
    from devflow.jobs.queue import Lease, Queue
    from devflow.jobs.schemas import ProbeResult

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "process-recovery")
    old, marker, _ = start_worker(env, tmp_path, "old", "blocked")
    new = None
    try:

        async def reached_provider():
            assert old.poll() is None, (
                "Owned Worker exited before reaching GitHub transport"
            )
            return marker.exists()

        await wait_until(reached_provider)
        async with env.factory() as session:
            state = await session.get(Job, job.id)
            assert state.status == "running" and state.attempt == 1
            old_lease = Lease(
                state.id, state.lease_owner, state.lease_generation, state.attempt
            )
        old.kill()
        await asyncio.to_thread(old.wait, timeout=10)
        assert old.returncode != 0
        # Deterministic stored boundary, independent of host/DB clock skew.
        async with env.factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE jobs SET lease_expires_at = '2000-01-01 UTC' WHERE id = :id"
                ),
                {"id": job.id},
            )
        new, _, new_stop = start_worker(env, tmp_path, "new", "ready")

        async def recovered():
            assert new.poll() is None, "Replacement Worker exited during recovery"
            async with env.factory() as session:
                row = await session.get(Job, job.id)
                return row.status == "retry_wait"

        await wait_until(recovered)
        async with env.factory.begin() as session:
            await session.execute(
                text("UPDATE jobs SET available_at = '2000-01-01 UTC' WHERE id = :id"),
                {"id": job.id},
            )

        async def succeeded():
            assert new.poll() is None, "Replacement Worker exited before completion"
            async with env.factory() as session:
                return (await session.get(Job, job.id)).status == "succeeded"

        await wait_until(succeeded)
        new_stop.write_text("stop")
        await asyncio.to_thread(new.wait, timeout=10)
        assert new.returncode == 0
        async with env.factory() as session:
            final = await session.get(Job, job.id)
            events = list(
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.job_id == job.id)
                    .order_by(RunEvent.sequence)
                )
            )
            audit_ids = list(
                await session.scalars(
                    select(AuditLog.id).where(AuditLog.job_id == job.id)
                )
            )
        assert final.attempt == 2
        assert [event.payload["status"] for event in events] == [
            "queued",
            "running",
            "retry_wait",
            "running",
            "succeeded",
        ]
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
        assert (
            await Queue(env.factory).complete(
                old_lease, ProbeResult.model_validate(final.result)
            )
            is False
        )
        async with env.factory() as session:
            assert (
                list(
                    await session.scalars(
                        select(AuditLog.id).where(AuditLog.job_id == job.id)
                    )
                )
                == audit_ids
            )
            assert (await session.get(Job, job.id)).last_sequence == 5
    finally:
        old_output = await reap(old)
        new_output = await reap(new) if new is not None else ("", "")
        for output in (*old_output, *new_output):
            assert "user-token" not in output
            assert "postgresql+psycopg" not in output


@pytest.mark.asyncio
async def test_run_worker_respects_configured_concurrency_and_caller_owned_http(
    job_env,
):
    from devflow.jobs.models import Job
    from devflow.worker import run_worker

    env = job_env
    jobs = [
        await env.jobs.submit_probe(
            env.actor, env.repository.id, f"concurrent-{number}"
        )
        for number in range(3)
    ]
    two_started = asyncio.Event()
    release = asyncio.Event()
    stop = asyncio.Event()
    requests = 0

    async def block(request):
        nonlocal requests
        if request.url.path == "/user/installations":
            requests += 1
            if requests == 2:
                two_started.set()
            await release.wait()

    env.before_response = block
    task = asyncio.create_task(
        run_worker(
            env.settings.model_copy(update={"worker_concurrency": 2}),
            stop=stop,
            github=env.github,
        )
    )
    try:
        async with asyncio.timeout(5):
            await two_started.wait()
        async with env.factory() as session:
            statuses = list(
                await session.scalars(
                    select(Job.status).where(Job.id.in_([job.id for job in jobs]))
                )
            )
        assert statuses.count("running") == 2
        assert statuses.count("queued") == 1
        stop.set()
        release.set()
        async with asyncio.timeout(5):
            await task
        env.before_response = None
        assert (await env.github.list_installations(SecretStr("user-token"), 1)).items[
            0
        ].id == 11
        async with env.factory() as session:
            statuses = list(
                await session.scalars(
                    select(Job.status).where(Job.id.in_([job.id for job in jobs]))
                )
            )
        assert statuses.count("succeeded") == 2
        assert statuses.count("queued") == 1
    finally:
        release.set()
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_worker_heartbeat_commits_while_provider_is_pending(job_env):
    from devflow.jobs.models import Job
    from devflow.worker import run_worker

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "heartbeat")
    started = asyncio.Event()
    release = asyncio.Event()
    stop = asyncio.Event()

    async def block(request):
        if request.url.path == "/user/installations":
            started.set()
            await release.wait()

    env.before_response = block
    task = asyncio.create_task(
        run_worker(
            env.settings.model_copy(update={"job_heartbeat_seconds": 1}),
            stop=stop,
            github=env.github,
        )
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
        async with env.factory.begin() as session:
            row = await session.get(Job, job.id, with_for_update=True)
            # A clearly future sentinel exposes the next committed renewal.
            sentinel = await session.scalar(
                select(func.clock_timestamp() + text("interval '1 hour'"))
            )
            row.lease_expires_at = sentinel

        async def renewed():
            async with env.factory() as session:
                row = await session.get(Job, job.id)
                return row.status == "running" and row.lease_expires_at != sentinel

        await wait_until(renewed, timeout=5)
        stop.set()
        release.set()
        async with asyncio.timeout(5):
            await task
        async with env.factory() as session:
            assert (await session.get(Job, job.id)).status == "succeeded"
    finally:
        release.set()
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
