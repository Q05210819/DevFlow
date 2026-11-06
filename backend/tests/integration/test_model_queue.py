"""Model-kind admission and in-process fairness against the real queue."""

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

BACKEND = Path(__file__).resolve().parents[2]
PROCESS = BACKEND / "tests" / "support" / "model_claim_process.py"


async def enqueue_model(
    env,
    kind: str,
    key: str,
    *,
    status: str = "queued",
    attempt: int = 0,
):
    from devflow.jobs.models import Job

    async with env.factory.begin() as session:
        job = Job(
            kind=kind,
            requested_by=env.actor.id,
            repository_id=env.repository.id,
            idempotency_key=key,
            input_hash="b" * 64,
            input={"version": 1},
            status=status,
            attempt=attempt,
            available_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        session.add(job)
        await session.flush()
        return job.id


async def make_due(env, job_id):
    async with env.factory.begin() as session:
        await session.execute(
            text("UPDATE jobs SET available_at = '2000-01-01 UTC' WHERE id = :id"),
            {"id": job_id},
        )


async def wait_for(path: Path, token: str, count: int = 1, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text_value = path.read_text(encoding="utf-8") if path.exists() else ""
        if text_value.count(token) >= count:
            return
        await asyncio.sleep(0.02)
    text_value = path.read_text(encoding="utf-8") if path.exists() else ""
    raise AssertionError(f"timed out waiting for {token!r} in {text_value!r}")


def start_process(config: Path, events: Path, release: Path):
    return subprocess.Popen(
        [
            sys.executable,
            str(PROCESS),
            str(config),
            str(events),
            str(release),
            "30",
        ],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.asyncio
async def test_kind_filter_applies_to_candidate_and_claim(job_env):
    from devflow.jobs.models import Job
    from devflow.jobs.queue import Queue

    env = job_env
    queue = Queue(env.factory)
    owner = uuid4()
    chat = await enqueue_model(env, "chat.run", "filter-chat")
    batch = await enqueue_model(env, "knowledge.index_batch", "filter-batch")

    # The default is the ordinary kind set, never every registered kind.
    assert await queue.claim(owner) is None

    lease = await queue.claim(owner, allowed_kinds=frozenset({"knowledge.index_batch"}))
    assert lease is not None
    assert lease.job_id == batch

    # A kind changed between candidate selection and the locked claim must not
    # be admitted under the old kind.
    changed = await enqueue_model(env, "chat.run", "filter-changed")
    async with env.factory.begin() as session:
        await session.execute(
            text("UPDATE jobs SET kind='knowledge.index_batch' WHERE id=:id"),
            {"id": changed},
        )
    assert (
        await queue._claim_one(
            owner,
            changed,
            "chat.run",
            env.repository.id,
            allowed_kinds=frozenset({"chat.run"}),
        )
        is None
    )
    lease = await queue.claim(owner, allowed_kinds=frozenset({"knowledge.index_batch"}))
    assert lease is not None
    assert lease.job_id == changed

    # retry_wait is still claimable under the explicit model filter.
    retry = await enqueue_model(
        env,
        "knowledge.query",
        "filter-retry",
        status="retry_wait",
        attempt=1,
    )
    lease = await queue.claim(owner, allowed_kinds=frozenset({"knowledge.query"}))
    assert lease is not None
    assert lease.job_id == retry

    async with env.factory() as session:
        assert (
            await session.scalar(select(Job.status).where(Job.id == chat)) == "queued"
        )


@pytest.mark.asyncio
async def test_ordinary_worker_never_claims_model_jobs(job_env):
    from devflow.jobs.queue import Queue
    from devflow.jobs.schemas import MODEL_JOB_KINDS

    env = job_env
    queue = Queue(env.factory)
    owner = uuid4()
    for index, kind in enumerate(sorted(MODEL_JOB_KINDS)):
        await enqueue_model(env, kind, f"ordinary-model-{index}")

    assert await queue.claim(owner) is None

    probe = await env.jobs.submit_probe(env.actor, env.repository.id, "ordinary-probe")
    await make_due(env, probe.id)
    lease = await queue.claim(owner)
    assert lease is not None
    assert lease.job_id == probe.id


@pytest.mark.asyncio
async def test_ten_interactive_then_one_background(job_env):
    from devflow.jobs.queue import Queue
    from devflow.jobs.schemas import (
        INTERACTIVE_MODEL_JOB_KINDS,
        MODEL_BACKGROUND_BATCH_CHUNKS,
        MODEL_INTERACTIVE_STREAK_LIMIT,
        MODEL_JOB_KINDS,
    )

    assert MODEL_INTERACTIVE_STREAK_LIMIT == 10
    assert MODEL_BACKGROUND_BATCH_CHUNKS == 16

    env = job_env
    queue = Queue(env.factory)
    owner = uuid4()
    kinds = {}
    for index in range(12):
        job_id = await enqueue_model(env, "knowledge.query", f"fair-query-{index}")
        kinds[job_id] = "knowledge.query"
    for index in range(2):
        job_id = await enqueue_model(
            env, "knowledge.index_batch", f"fair-background-{index}"
        )
        kinds[job_id] = "knowledge.index_batch"

    leases = []
    for _ in range(14):
        lease = await queue.claim(
            owner,
            allowed_kinds=MODEL_JOB_KINDS,
            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
        )
        assert lease is not None
        leases.append(lease)

    assert [kinds[lease.job_id] for lease in leases[:10]] == ["knowledge.query"] * 10
    assert kinds[leases[10].job_id] == "knowledge.index_batch"
    assert [kinds[lease.job_id] for lease in leases[11:13]] == [
        "knowledge.query",
        "knowledge.query",
    ]
    assert kinds[leases[13].job_id] == "knowledge.index_batch"
    assert (
        await queue.claim(
            owner,
            allowed_kinds=MODEL_JOB_KINDS,
            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
        )
        is None
    )


@pytest.mark.asyncio
async def test_interactive_continues_without_background(job_env):
    from devflow.jobs.queue import Queue
    from devflow.jobs.schemas import INTERACTIVE_MODEL_JOB_KINDS, MODEL_JOB_KINDS

    env = job_env
    queue = Queue(env.factory)
    owner = uuid4()
    kinds = {}
    for index in range(12):
        job_id = await enqueue_model(env, "knowledge.query", f"no-bg-query-{index}")
        kinds[job_id] = "knowledge.query"

    leases = [
        await queue.claim(
            owner,
            allowed_kinds=MODEL_JOB_KINDS,
            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
        )
        for _ in range(12)
    ]
    assert all(lease is not None for lease in leases)
    assert [kinds[lease.job_id] for lease in leases] == ["knowledge.query"] * 12
    assert (
        await queue.claim(
            owner,
            allowed_kinds=MODEL_JOB_KINDS,
            prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
        )
        is None
    )


@pytest.mark.asyncio
async def test_two_model_owner_processes_only_one_claims(job_env, workspace_tmp_path):
    env = job_env
    await enqueue_model(env, "knowledge.query", "process-claim")
    embedding = workspace_tmp_path / "bge-m3"
    reranker = workspace_tmp_path / "bge-reranker-v2-m3"
    embedding.mkdir()
    reranker.mkdir()
    config = workspace_tmp_path / "settings.json"
    events = workspace_tmp_path / "events.txt"
    first_release = workspace_tmp_path / "first.release"
    second_release = workspace_tmp_path / "second.release"
    config.write_text(
        json.dumps(
            {
                "app_env": "test",
                "public_base_url": "http://localhost:3000",
                "database_url": env.settings.database_url.get_secret_value(),
                "model_embedding_path": str(embedding),
                "model_reranker_path": str(reranker),
                "model_lock_path": str(workspace_tmp_path / "model.lock"),
                "worker_role": "model",
                "model_device": "cpu",
                "model_dtype": "float32",
            }
        ),
        encoding="utf-8",
    )

    first = start_process(config, events, first_release)
    second = None
    try:
        await wait_for(events, "claimed")
        second = start_process(config, events, second_release)
        _, error = await asyncio.to_thread(second.communicate, 15)
        assert second.returncode == 0, error
        text_value = events.read_text(encoding="utf-8")
        assert text_value.count("claimed") == 1
        assert "busy" in text_value
        assert text_value.count("load_embedding") == 1
    finally:
        if second is not None and second.poll() is None:
            second.terminate()
            await asyncio.to_thread(second.communicate, 15)
        first_release.touch()
        await asyncio.to_thread(first.communicate, 15)
    assert "released" in events.read_text(encoding="utf-8")
