"""Real isolated PostgreSQL for the official LangGraph checkpoint saver.

The saver schema is deployed explicitly, with an autocommit connection, exactly
as the controlled ``python -m devflow.chat.checkpoints --setup`` step does.
Tests never substitute an in-memory saver.
"""

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from devflow.chat.models import AgentRun
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.knowledge.schemas import IndexSelection, QuerySelection
from devflow.sync.scope import transaction
from support.chat_env import chat_env as chat_env  # noqa: PLC0414
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414


def raw_dsn(settings) -> str:
    """The same scoped URL without SQLAlchemy's driver prefix."""

    return settings.database_url.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@asynccontextmanager
async def connect(settings):
    # Official LangGraph usage: autocommit so each saver transaction is a real
    # top-level unit, never a savepoint inside an implicit transaction.
    connection = await psycopg.AsyncConnection.connect(
        raw_dsn(settings), autocommit=True
    )
    try:
        yield connection
    finally:
        await connection.close()


async def setup_saver_schema(settings) -> None:
    async with await psycopg.AsyncConnection.connect(
        raw_dsn(settings), autocommit=True
    ) as connection:
        await AsyncPostgresSaver(connection).setup()


def checkpoint(channel_values=None, *, checkpoint_id=None) -> dict:
    values = dict(channel_values or {})
    # A real graph advances the version of every updated channel, so each
    # checkpoint owns its own blob rows. Reusing one version would collapse the
    # blobs on the official primary key.
    version = uuid4().hex
    return {
        "v": 1,
        "id": str(checkpoint_id or uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": values,
        "channel_versions": {key: version for key in values},
        "versions_seen": {},
        "updated_channels": list(values),
    }


def config(thread_id: UUID, checkpoint_id=None) -> dict:
    configurable = {"thread_id": str(thread_id), "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = str(checkpoint_id)
    return {"configurable": configurable}


def versions(checkpoint_value: dict) -> dict:
    return dict(checkpoint_value["channel_versions"])


def selection(*repository_ids):
    ids = tuple(repository_ids)
    return QuerySelection(
        selected_repository_ids=ids,
        indexes=(IndexSelection(repository_id=ids[0], sha="a" * 40, target="target"),),
    )


async def create_run(env, label):
    view = await env.chat.create(env.actor, (env.repository.id,), f"{label}-create")
    return await env.chat.submit(
        env.actor,
        view.id,
        selection(env.repository.id),
        f"question {label}",
        f"{label}-run",
    )


async def claim_chat(env):
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=frozenset({"chat.run"})
    )
    assert lease is not None
    return lease


async def active_run(env, label):
    receipt = await create_run(env, label)
    lease = await claim_chat(env)
    assert lease.job_id == receipt.job_id
    async with env.factory() as db:
        run = await db.scalar(select(AgentRun).where(AgentRun.id == receipt.run_id))
    assert run is not None and run.job_id == lease.job_id
    grant = await env.chat.access.verify(env.actor, [env.repository.id], fresh=True)
    return SimpleNamespace(
        run_id=receipt.run_id,
        thread_id=run.thread_id,
        lease=lease,
        grant=grant,
    )


async def enqueue_cleanup(env, *, kind="chat.cleanup"):
    async with env.factory.begin() as db:
        job = Job(
            kind=kind,
            repository_id=env.repository.id,
            requested_by=None,
            input={"version": 1, "limit": 100},
            input_hash=hashlib.sha256(b"chat-cleanup").hexdigest(),
            idempotency_key=uuid4().hex,
        )
        db.add(job)
        await db.flush()
        return job.id


async def claim_cleanup(env):
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=frozenset({"chat.cleanup"})
    )
    assert lease is not None
    return lease


async def finish_run(env, lease, run_id, outcome):
    async with transaction(env.factory) as db:
        await env.chat.finish(db, lease, run_id, outcome)


async def official_counts(connection, thread_id):
    async with connection.cursor() as cursor:
        counts = []
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            await cursor.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = %s",
                (str(thread_id),),
            )
            counts.append((await cursor.fetchone())[0])
    return tuple(counts)


@pytest_asyncio.fixture
async def checkpoint_env(chat_env):
    env = chat_env
    await setup_saver_schema(env.settings)
    return env
