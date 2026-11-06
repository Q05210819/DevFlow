"""Real PostgreSQL fences around the official LangGraph checkpoint saver.

Every operation runs in one explicit transaction that also writes the accepted
config: an injected failure rolls back official rows, blobs and accepted state
together. No in-memory saver stands in for the official package.
"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.checkpoint_env import (
    active_run,
    checkpoint,
    config,
    connect,
    official_counts,
    versions,
)
from support.checkpoint_env import (
    checkpoint_env as checkpoint_env,  # noqa: PLC0414
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.chat import checkpoints
from devflow.chat.checkpoints import FencedPostgresSaver
from devflow.chat.models import AgentRun
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue

pytestmark = pytest.mark.asyncio


async def expire(env, lease):
    async with env.factory.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id == lease.job_id)
            .values(lease_expires_at=text("clock_timestamp() - interval '1 second'"))
        )


async def test_aput_and_accepted_rollback_together(checkpoint_env, monkeypatch):
    env = checkpoint_env
    run = await active_run(env, "rollback")
    checkpoint_id = uuid4()
    stored = checkpoint(
        {"messages": [{"role": "user", "content": "secret"}]},
        checkpoint_id=checkpoint_id,
    )

    async def fail_accepted(cursor, run_id, accepted):
        raise AppError("internal_error", 500, "Injected accepted failure")

    monkeypatch.setattr(checkpoints, "_store_accepted", fail_accepted)

    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        with pytest.raises(AppError) as error:
            await saver.aput(
                config(run.thread_id), stored, {"step": 1}, versions(stored)
            )
        assert error.value.code == "internal_error"
        assert await official_counts(connection, run.thread_id) == (0, 0, 0)

    async with env.factory() as db:
        row = await db.scalar(select(AgentRun).where(AgentRun.id == run.run_id))
    assert row is not None and row.accepted_config is None


async def test_aput_writes_is_fenced(checkpoint_env):
    env = checkpoint_env

    async def prepared(connection, label):
        run = await active_run(env, label)
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        stored = checkpoint({"messages": []})
        await saver.aput(config(run.thread_id), stored, {"step": 1}, versions(stored))
        return run, saver

    def write(saver, run, value="current"):
        return saver.aput_writes(
            config(run.thread_id), [("messages", {"value": value})], f"task-{value}"
        )

    async with connect(env.settings) as connection:
        run, saver = await prepared(connection, "owner")
        await write(saver, run)
        before = await official_counts(connection, run.thread_id)
        stale_owners = (
            replace(run.lease, owner=uuid4()),
            replace(run.lease, generation=run.lease.generation + 1),
            replace(run.lease, attempt=run.lease.attempt + 1),
        )
        for stale in stale_owners:
            stale_saver = FencedPostgresSaver(connection, stale, run.run_id, run.grant)
            with pytest.raises(AppError) as error:
                await write(stale_saver, run, "stale")
            assert error.value.code == "lease_expired"
            assert await official_counts(connection, run.thread_id) == before

    async with connect(env.settings) as connection:
        run, saver = await prepared(connection, "expired")
        await expire(env, run.lease)
        with pytest.raises(AppError) as error:
            await write(saver, run, "expired")
        assert error.value.code == "lease_expired"
        assert await official_counts(connection, run.thread_id) == (1, 1, 0)

    async with connect(env.settings) as connection:
        run, saver = await prepared(connection, "revoked")
        await env.revoke(env.repository.id)
        with pytest.raises(AppError) as error:
            await write(saver, run, "revoked")
        assert error.value.code == "resource_not_found"
        assert await official_counts(connection, run.thread_id) == (1, 1, 0)


async def test_expiry_while_waiting_for_lock(checkpoint_env):
    env = checkpoint_env
    run = await active_run(env, "lock-wait")
    stored = checkpoint({"messages": []})

    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        task = asyncio.create_task(
            saver.aput(config(run.thread_id), stored, {"step": 1}, versions(stored))
        )
        async with env.factory() as db:
            await db.execute(
                select(Job).where(Job.id == run.lease.job_id).with_for_update()
            )
            await asyncio.sleep(0.4)
            await db.execute(
                update(Job)
                .where(Job.id == run.lease.job_id)
                .values(
                    lease_expires_at=text("clock_timestamp() - interval '1 second'")
                )
            )
            await db.commit()
        with pytest.raises(AppError) as error:
            await asyncio.wait_for(task, timeout=5)
        assert error.value.code == "lease_expired"
        assert await official_counts(connection, run.thread_id) == (0, 0, 0)


async def test_pipeline_and_savepoint_do_not_commit_outer_transaction(checkpoint_env):
    env = checkpoint_env
    for label, pipeline in (("pipeline", True), ("savepoint", False)):
        run = await active_run(env, label)
        stored = checkpoint({"messages": []})
        async with connect(env.settings) as connection:
            saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
            saver._saver.supports_pipeline = pipeline
            with pytest.raises(RuntimeError):
                async with connection.transaction():
                    await saver.aput(
                        config(run.thread_id), stored, {"step": 1}, versions(stored)
                    )
                    raise RuntimeError("force rollback")
            assert await official_counts(connection, run.thread_id) == (0, 0, 0)
        async with env.factory() as db:
            row = await db.scalar(select(AgentRun).where(AgentRun.id == run.run_id))
        assert row is not None and row.accepted_config is None


async def test_exact_checkpoint_load_includes_legal_pending_writes(checkpoint_env):
    env = checkpoint_env
    run = await active_run(env, "pending")
    checkpoint_id = uuid4()
    stored = checkpoint({"messages": []}, checkpoint_id=checkpoint_id)

    async with connect(env.settings) as connection:
        saver = FencedPostgresSaver(connection, run.lease, run.run_id, run.grant)
        accepted = await saver.aput(
            config(run.thread_id), stored, {"step": 1}, versions(stored)
        )
        await saver.aput_writes(accepted, [("messages", {"text": "legal"})], "task-1")
        loaded = await saver.aget_tuple(
            config(run.thread_id, checkpoint_id=checkpoint_id)
        )
        assert loaded is not None
        assert loaded.config["configurable"]["checkpoint_id"] == str(checkpoint_id)
        assert loaded.config["configurable"]["checkpoint_ns"] == ""
        assert [
            (task, channel, value) for task, channel, value in loaded.pending_writes
        ] == [("task-1", "messages", {"text": "legal"})]

        with pytest.raises(AppError) as error:
            await saver.aget_tuple(config(run.thread_id, checkpoint_id=uuid4()))
        assert error.value.code == "validation_error"
        with pytest.raises(AppError) as error:
            await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": str(run.thread_id),
                        "checkpoint_ns": "attempt-2",
                    }
                }
            )
        assert error.value.code == "validation_error"
        with pytest.raises(AppError) as error:
            await saver.aput_writes(
                {
                    "configurable": {
                        "thread_id": str(run.thread_id),
                        "checkpoint_ns": "attempt-2",
                        "checkpoint_id": str(checkpoint_id),
                    }
                },
                [("messages", {"text": "bad"})],
                "task-bad",
            )
        assert error.value.code == "validation_error"

        await expire(env, run.lease)
        queue = Queue(
            env.factory,
            lease_seconds=300,
            retry_first_seconds=0,
            retry_second_seconds=0,
        )
        assert await queue.recover_expired() == 1
        async with env.factory() as db:
            refreshed = await db.scalar(
                select(AgentRun).where(AgentRun.id == run.run_id)
            )
        assert refreshed is not None and refreshed.terminal_at is None
        lease = await Queue(env.factory, lease_seconds=300).claim(
            uuid4(), allowed_kinds=frozenset({"chat.run"})
        )
        assert lease is not None and lease.generation > run.lease.generation

        current = FencedPostgresSaver(connection, lease, run.run_id, run.grant)
        with pytest.raises(AppError) as error:
            await saver.aput_writes(
                accepted, [("messages", {"text": "stale"})], "task-stale"
            )
        assert error.value.code == "lease_expired"
        await current.aput_writes(
            accepted, [("messages", {"text": "second"})], "task-2"
        )
        loaded = await current.aget_tuple(
            config(run.thread_id, checkpoint_id=checkpoint_id)
        )
        assert loaded is not None
        tasks = {task for task, _, _ in loaded.pending_writes}
        assert tasks == {"task-1", "task-2"}
        assert all(channel == "messages" for _, channel, _ in loaded.pending_writes)

        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = %s",
                (str(run.thread_id),),
            )
            assert {row[0] for row in await cursor.fetchall()} == {""}
            await cursor.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s",
                (str(run.thread_id),),
            )
            ids = [row[0] for row in await cursor.fetchall()]
        assert len(ids) == 1
        assert str(uuid4().__class__(ids[0])) == ids[0]
