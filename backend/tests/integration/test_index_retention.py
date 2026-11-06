"""Bounded vector retention: keep one active/building/newest retired per repo."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.index_env import (
    FakeMilvus,
    IndexVersion,
    make_index,
    ready_import,
    set_active,
)
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_knowledge_retention import cleanup_job

from devflow.jobs.queue import Queue
from devflow.knowledge.index_retention import (
    GLOBAL_MEMBER_CAP,
    RETIRED_TTL_DAYS,
    IndexRetention,
)
from devflow.knowledge.models import Chunk, DocumentRevision, RevisionRetention

pytestmark = pytest.mark.asyncio


async def run_index_sweep(env, fake, *, limit=100):
    job_id = await cleanup_job(env, limit=limit)
    await env.due(job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert lease is not None and lease.job_id == job_id
    result = await IndexRetention(env.factory, index=fake).sweep(lease, limit=limit)
    return job_id, lease, result


async def state(env, index_id):
    async with env.factory() as db:
        row = await db.get(IndexVersion, index_id)
        return row


async def test_per_repository_keeps_active_building_and_newest_retired(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    active = await make_index(env, fixture, status="ready", created_days_ago=30)
    await set_active(env, active.id)
    building = await make_index(env, fixture, status="building")
    newest = await make_index(env, fixture, status="retired", created_days_ago=1)
    middle = await make_index(env, fixture, status="retired", created_days_ago=2)
    expired = await make_index(env, fixture, status="retired", created_days_ago=10)

    fake = FakeMilvus()
    _, _, result = await run_index_sweep(env, fake)
    assert result.examined >= 5
    assert result.purged == 2
    assert set(fake.deleted) == {middle.id, expired.id}

    assert (await state(env, active.id)).purged_at is None
    assert (await state(env, active.id)).queryable is True
    assert (await state(env, building.id)).purged_at is None
    assert (await state(env, newest.id)).purged_at is None
    for purged in (middle, expired):
        row = await state(env, purged.id)
        assert row.purged_at is not None
        assert row.queryable is False


async def test_global_cap_purges_retired_but_never_active_or_building(
    knowledge_env, monkeypatch
):
    assert GLOBAL_MEMBER_CAP == 120_000
    assert RETIRED_TTL_DAYS == 7
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    active = await make_index(
        env, fixture, status="ready", chunk_ids=[uuid4() for _ in range(4)]
    )
    await set_active(env, active.id)
    building = await make_index(
        env, fixture, status="building", chunk_ids=[uuid4() for _ in range(4)]
    )
    newest = await make_index(
        env,
        fixture,
        status="retired",
        chunk_ids=[uuid4() for _ in range(4)],
        created_days_ago=1,
    )
    older = await make_index(
        env,
        fixture,
        status="retired",
        chunk_ids=[uuid4() for _ in range(4)],
        created_days_ago=2,
    )

    import devflow.knowledge.index_retention as retention

    monkeypatch.setattr(retention, "GLOBAL_MEMBER_CAP", 10)
    fake = FakeMilvus()
    _, _, result = await run_index_sweep(env, fake)
    assert result.purged == 2
    assert set(fake.deleted) == {newest.id, older.id}
    for kept in (active, building):
        assert (await state(env, kept.id)).purged_at is None
    assert (await state(env, active.id)).queryable is True


async def test_delete_failure_is_deferred_and_retried(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    oldest = await make_index(env, fixture, status="retired", created_days_ago=10)
    fake = FakeMilvus()
    fake.fail_deletes = 1
    _, _, first = await run_index_sweep(env, fake)
    assert first.purged == 0
    assert first.deferred == 1
    row = await state(env, oldest.id)
    assert row.purged_at is None
    assert row.queryable is False
    assert fake.deleted == []

    _, _, second = await run_index_sweep(env, fake)
    assert second.purged == 1
    assert second.deferred == 0
    assert fake.deleted == [oldest.id]
    assert (await state(env, oldest.id)).purged_at is not None


async def test_failed_build_vectors_are_purged_even_without_recorded_writes(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    failed = await make_index(env, fixture, status="failed", chunk_ids=[])
    fake = FakeMilvus()
    # A batch can upsert vectors and crash before the counter is recorded, so
    # the sweep must not trust written_count for a failed build.
    fake.rows[failed.id] = {uuid4(): object()}

    _, _, result = await run_index_sweep(env, fake)

    assert result.purged == 1
    assert fake.deleted == [failed.id]
    assert failed.id not in fake.rows
    row = await state(env, failed.id)
    assert row.purged_at is not None
    assert row.queryable is False


async def test_missing_collection_counts_as_purged_without_recreating_it(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    oldest = await make_index(env, fixture, status="retired", created_days_ago=10)
    fake = FakeMilvus()
    fake.collection_present = False

    _, _, result = await run_index_sweep(env, fake)

    assert result.purged == 1
    assert result.deferred == 0
    assert fake.schema_calls == 0
    assert fake.deleted == []
    assert (await state(env, oldest.id)).purged_at is not None


async def test_retained_retired_version_is_chosen_by_retirement_time(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    old_build_recently_retired = await make_index(
        env, fixture, status="retired", created_days_ago=5
    )
    recent_build_retired_earlier = await make_index(
        env, fixture, status="retired", created_days_ago=1
    )
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        row = await db.get(IndexVersion, old_build_recently_retired.id)
        row.retired_at = now - timedelta(hours=1)
        row = await db.get(IndexVersion, recent_build_retired_earlier.id)
        row.retired_at = now - timedelta(days=1)
    fake = FakeMilvus()

    _, _, result = await run_index_sweep(env, fake)

    assert result.purged == 1
    assert fake.deleted == [recent_build_retired_earlier.id]
    assert (await state(env, old_build_recently_retired.id)).purged_at is None


async def test_purge_never_touches_canonical_bodies_or_pinned_revisions(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    oldest = await make_index(env, fixture, status="retired", created_days_ago=10)
    revision_id = fixture.revision_ids[0]
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            RevisionRetention(
                owner_kind="message",
                owner_id=uuid4(),
                revision_id=revision_id,
                expires_at=now,
            )
        )
    fake = FakeMilvus()
    _, _, result = await run_index_sweep(env, fake)
    assert result.purged == 1
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        chunks = await db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.revision_id == revision_id)
        )
        pinned = await db.scalar(
            select(RevisionRetention).where(
                RevisionRetention.revision_id == revision_id
            )
        )
    assert revision is not None and revision.body is not None
    assert chunks == len(fixture.chunk_ids)
    assert pinned is not None
    assert (await state(env, oldest.id)).purged_at is not None
