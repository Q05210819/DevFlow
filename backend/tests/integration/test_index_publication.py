"""Batched index construction, fencing, and atomic active publication."""

import os
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from support.index_env import (
    SHA,
    FakeInference,
    FakeMilvus,
    IndexMember,
    IndexVersion,
    digest,
    fingerprint,
    make_index,
    ready_import,
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_knowledge_chunking import chunker as chunker  # noqa: PLC0414
from test_knowledge_retention import finish, upload

from devflow.auth.models import User
from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import INTERACTIVE_MODEL_JOB_KINDS, MODEL_JOB_KINDS
from devflow.knowledge.indexing import IndexBuilder, VisibilityProof
from devflow.knowledge.milvus import MilvusIndex
from devflow.knowledge.models import (
    Chunk,
    DocumentRevision,
    ImportMember,
    KnowledgeCorpus,
    KnowledgeImport,
)
from devflow.knowledge.schemas import IndexReceipt
from devflow.knowledge.service import KnowledgeService
from devflow.repositories.models import Repository

pytestmark = pytest.mark.asyncio

MILVUS_URI = os.environ.get("TEST_MILVUS_URI")


def builder(env, *, inference=None, index=None) -> IndexBuilder:
    return IndexBuilder(
        KnowledgeService(env.factory, env.policy),
        inference=inference or FakeInference(),
        index=index or FakeMilvus(),
    )


async def request_and_claim(env, *, fixture, fake, inference=None, key=None):
    build = builder(env, inference=inference, index=fake)
    receipt = await build.request(
        env.actor, fixture.import_id, key or f"index-{uuid4().hex}"
    )
    assert isinstance(receipt, IndexReceipt)
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(),
        allowed_kinds=MODEL_JOB_KINDS,
        prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
    )
    assert lease is not None and lease.job_id == receipt.job_id
    return build, receipt, lease


async def proof_for(env, index_id) -> VisibilityProof:
    async with env.factory() as db:
        row = await db.get(IndexVersion, index_id)
        observed = await db.scalar(select(func.clock_timestamp()))
        return VisibilityProof(
            index_id=index_id,
            member_digest=row.member_digest,
            fingerprint=fingerprint(),
            observed_at=observed,
        )


async def build_to_pending(env, *, chunk_count=2):
    fixture = await ready_import(env, chunk_count=chunk_count)
    fake = FakeMilvus()
    fake.visible = False
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)
    with pytest.raises(AppError) as raised:
        await build.run_batch(lease)
    assert raised.value.code == "service_unavailable"
    fake.visible = True
    return fixture, fake, build, receipt, lease, await proof_for(env, receipt.index_id)


async def index_state(env, index_id):
    async with env.factory() as db:
        row = await db.get(IndexVersion, index_id)
        job = await db.get(Job, row.job_id)
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        return row, job, corpus


async def test_publish_switches_active_and_retires_previous(knowledge_env):
    env = knowledge_env
    first = await ready_import(env, chunk_count=2)
    fake = FakeMilvus()
    build = builder(env, index=fake)
    receipt = await build.request(env.actor, first.import_id, "first-build")
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    await build.run_batch(lease)
    old, job, corpus = await index_state(env, receipt.index_id)
    assert (old.status, old.queryable) == ("ready", True)
    assert corpus.active_index_version_id == old.id
    assert job.status == "succeeded"

    second = await ready_import(env, generation=2, sha="c" * 40, chunk_count=2)
    new_receipt = await build.request(env.actor, second.import_id, "second-build")
    await env.due(new_receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    await build.run_batch(lease)
    new, new_job, corpus = await index_state(env, new_receipt.index_id)
    old, old_job, _ = await index_state(env, receipt.index_id)
    assert (new.status, new.queryable) == ("ready", True)
    assert corpus.active_index_version_id == new.id
    assert new_job.status == "succeeded"
    assert (old.status, old.queryable) == ("retired", False)
    assert old.retired_at is not None
    assert old_job.status == "succeeded"
    # Retirement keeps the vector version for queries pinned to it.
    assert set(fake.rows) == {old.id, new.id}


async def test_active_requires_current_generation_full_fence_and_valid_lease(
    knowledge_env,
):
    env = knowledge_env

    async def mutate(case):
        async with env.factory.begin() as db:
            if case == "user_version":
                (await db.get(User, env.actor.id)).auth_version += 1
            elif case == "installation_version":
                installation = await db.scalar(
                    select(GitHubInstallation).where(
                        GitHubInstallation.installation_id
                        == env.repository.installation_id
                    )
                )
                installation.auth_version += 1
            elif case == "repository_version":
                (await db.get(Repository, env.repository.id)).auth_version += 1
            elif case in {"owner", "generation", "attempt", "expiry"}:
                row = await db.get(IndexVersion, receipt.index_id)
                job = await db.get(Job, row.job_id, with_for_update=True)
                if case == "owner":
                    job.lease_owner = uuid4()
                elif case == "attempt":
                    job.attempt += 1
                elif case == "expiry":
                    # The host clock is known to jump; fence against PG time.
                    job.lease_expires_at = await db.scalar(
                        select(func.clock_timestamp())
                    ) - timedelta(seconds=1)
                else:
                    corpus = await db.get(KnowledgeCorpus, env.repository.id)
                    corpus.target_generation += 1
            elif case == "same_sha_generation":
                corpus = await db.get(KnowledgeCorpus, env.repository.id)
                corpus.target_generation += 1
                assert corpus.target_sha == SHA
            elif case == "source_invalidated":
                revision = await db.get(DocumentRevision, fixture.revision_ids[0])
                revision.searchable = False
            else:  # pragma: no cover - parameter guard
                raise AssertionError(case)

    async def current_versions():
        async with env.factory() as db:
            user = await db.get(User, env.actor.id)
            installation = await db.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.installation_id == env.repository.installation_id
                )
            )
            repository = await db.get(Repository, env.repository.id)
            return (
                user.auth_version,
                installation.auth_version,
                repository.auth_version,
            )

    async def restore_versions(saved):
        env.actor = replace(env.actor, auth_version=saved[0])
        async with env.factory.begin() as db:
            (await db.get(User, env.actor.id)).auth_version = saved[0]
            installation = await db.scalar(
                select(GitHubInstallation).where(
                    GitHubInstallation.installation_id == env.repository.installation_id
                )
            )
            installation.auth_version = saved[1]
            (await db.get(Repository, env.repository.id)).auth_version = saved[2]

    cases = [
        "user_version",
        "installation_version",
        "repository_version",
        "owner",
        "generation",
        "attempt",
        "expiry",
        "same_sha_generation",
        "source_invalidated",
    ]
    # The fence covers every case independently against the same published
    # candidate: rebuild the pending state for each mutation.
    baseline = await current_versions()
    for case in cases:
        fixture, _fake, build, receipt, lease, proof = await build_to_pending(env)
        await mutate(case)
        allowed = await build.publish(lease, receipt.index_id, proof)
        row, job, corpus = await index_state(env, receipt.index_id)
        assert allowed is False, case
        assert corpus.active_index_version_id is None, case
        assert row.queryable is False, case
        if case in {"owner", "attempt", "expiry"}:
            # A lost fence is not evidence of supersession: a recovered owner
            # may still publish this candidate. The stale executor must leave
            # both the row and the Job untouched for that owner.
            assert row.status == "building", case
            assert job.status == "running", case
        else:
            assert row.status == "superseded", case
            assert job.status == "succeeded", case
        # Authorization versions are global to the fixture; later cases must
        # start from the same baseline they are meant to fence against.
        await restore_versions(baseline)


async def test_visibility_is_required_before_publication(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    fake = FakeMilvus()
    fake.visible = False
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)

    with pytest.raises(AppError) as raised:
        await build.run_batch(lease)
    assert raised.value.code == "service_unavailable"
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.written_count == 2
    assert corpus.active_index_version_id is None
    assert job.status == "running"

    # The same lease may retry; no member is written twice and nothing becomes
    # active while visibility is still missing.
    with pytest.raises(AppError):
        await build.run_batch(lease)
    row, _, _ = await index_state(env, receipt.index_id)
    assert row.written_count == 2

    fake.visible = True
    await build.run_batch(lease)
    row, job, corpus = await index_state(env, receipt.index_id)
    assert (row.status, row.queryable) == ("ready", True)
    assert corpus.active_index_version_id == row.id
    assert job.status == "succeeded"


async def test_external_write_then_db_rollback_is_unpublished(
    knowledge_env, monkeypatch
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    fake = FakeMilvus()
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)

    from devflow.knowledge import indexing

    original = indexing._record_batch
    calls = 0

    async def explode(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("db rollback after external write")

    monkeypatch.setattr(indexing, "_record_batch", explode)
    with pytest.raises(RuntimeError):
        await build.run_batch(lease)
    assert calls == 1
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.written_count == 0
    assert corpus.active_index_version_id is None
    assert job.status == "running"
    assert set(fake.rows[receipt.index_id]) == set(fixture.chunk_ids)
    async with env.factory() as db:
        pending = list(
            await db.scalars(
                select(IndexMember.status).where(
                    IndexMember.index_version_id == receipt.index_id
                )
            )
        )
    assert pending == ["pending", "pending"]

    monkeypatch.setattr(indexing, "_record_batch", original)
    await build.run_batch(lease)
    row, job, corpus = await index_state(env, receipt.index_id)
    assert (row.status, row.queryable) == ("ready", True)
    assert row.written_count == 2
    assert corpus.active_index_version_id == row.id
    assert job.status == "succeeded"
    assert set(fake.rows[receipt.index_id]) == set(fixture.chunk_ids)


async def test_batch_restart_preserves_counts_fixed_deadline_and_attempts(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=32)
    fake = FakeMilvus()
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)
    row, job, corpus = await index_state(env, receipt.index_id)
    created_at, deadline = row.created_at, fixture.deadline
    index_deadline = row.deadline
    assert index_deadline == created_at + timedelta(hours=24)
    assert row.failures == 0
    expected_digest = row.member_digest

    await build.run_batch(lease)
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.written_count == 16
    assert row.expected_member_count == 32
    assert row.member_digest == expected_digest
    assert row.created_at == created_at
    async with env.factory() as db:
        imported = await db.get(KnowledgeImport, fixture.import_id)
        assert imported.deadline == deadline
        successor = await db.scalar(
            select(Job).where(
                Job.kind == "knowledge.index_batch",
                Job.status == "queued",
                Job.input["index_id"].astext == str(receipt.index_id),
            )
        )
    assert successor is not None
    assert job.status == "succeeded" and job.attempt == 1
    assert corpus.active_index_version_id is None

    restarted = builder(env, inference=FakeInference(), index=fake)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == successor.id
    await restarted.run_batch(lease)
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.written_count == 32
    assert (row.status, row.queryable) == ("ready", True)
    assert row.deadline == index_deadline
    assert row.failures == 0
    assert corpus.active_index_version_id == row.id
    assert job.status == "succeeded" and job.attempt == 1


async def test_expired_build_window_fails_the_candidate_and_job(knowledge_env):
    env = knowledge_env
    _fixture, _fake, build, receipt, lease, _ = await build_to_pending(env)
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        row = await db.get(IndexVersion, receipt.index_id, with_for_update=True)
        # Keep the fixed-window invariant while moving the build 25 hours back.
        row.created_at = now - timedelta(hours=25)
        row.deadline = row.created_at + timedelta(hours=24)

    await build.run_batch(lease)

    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "failed"
    assert row.queryable is False
    assert job.status == "failed"
    assert job.error_code == "build_deadline_exceeded"
    assert corpus.active_index_version_id is None


async def test_build_failures_are_cumulative_across_successor_jobs(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=32)
    fake = FakeMilvus()
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)
    queue = Queue(env.factory)

    # The first Job fails once, then completes its half of the build.
    assert await queue.fail(lease, "attempt_timeout", True) is True
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == receipt.job_id
    await build.run_batch(lease)
    row, first_job, _ = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.failures == 1
    assert first_job.status == "succeeded"

    async with env.factory() as db:
        successor_id = await db.scalar(
            select(Job.id).where(
                Job.kind == "knowledge.index_batch",
                Job.status == "queued",
                Job.input["index_id"].astext == str(receipt.index_id),
            )
        )
    assert successor_id is not None
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == successor_id

    # A successor Job has a fresh attempt budget; the build counter does not.
    assert await queue.fail(lease, "attempt_timeout", True) is True
    row, _, _ = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.failures == 2

    await env.due(successor_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == successor_id
    assert await queue.fail(lease, "attempt_timeout", True) is True

    async with env.factory() as db:
        successor = await db.get(Job, successor_id)
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        assert successor is not None
        assert successor.status == "failed"
        assert successor.error_code == "attempt_timeout"
        assert corpus.active_index_version_id is None
    row, _, _ = await index_state(env, receipt.index_id)
    assert row.failures == 3
    assert row.status == "failed"
    assert row.queryable is False


async def test_publish_rechecks_the_lease_after_manifest_validation(
    knowledge_env, monkeypatch
):
    env = knowledge_env
    _fixture, _fake, build, receipt, lease, _ = await build_to_pending(env)

    from devflow.knowledge import indexing

    original = indexing._manifest

    async def expire_during_validation(db, imported):
        chunks = await original(db, imported)
        # Mutate the locked in-session row: the manifest step can outlive the
        # lease even though no other transaction can change the locked Job.
        now = await db.scalar(select(func.clock_timestamp()))
        job = await db.get(Job, lease.job_id)
        job.lease_expires_at = now - timedelta(seconds=1)
        return chunks

    monkeypatch.setattr(indexing, "_manifest", expire_during_validation)

    published = await build.publish(
        lease, receipt.index_id, await proof_for(env, receipt.index_id)
    )

    assert published is False
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.queryable is False
    assert job.status == "running"
    assert corpus.active_index_version_id is None


async def test_two_concurrent_builds_only_current_version_enters_building(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    fake = FakeMilvus()
    build = builder(env, index=fake)
    first = await build.request(env.actor, fixture.import_id, "request-a")
    second = await build.request(env.actor, fixture.import_id, "request-b")
    async with env.factory() as db:
        old = await db.get(IndexVersion, first.index_id)
        old_job = await db.get(Job, old.job_id)
        new = await db.get(IndexVersion, second.index_id)
    assert old.status == "superseded"
    assert old.queryable is False
    assert old_job.status == "succeeded"
    assert new.status == "queued"

    await env.due(second.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == second.job_id
    await build.run_batch(lease)
    row, job, corpus = await index_state(env, second.index_id)
    assert (row.status, row.queryable) == ("ready", True)
    assert corpus.active_index_version_id == row.id
    assert job.status == "succeeded"
    async with env.factory() as db:
        assert (await db.get(IndexVersion, first.index_id)).status == "superseded"


async def test_database_forbids_two_unfinished_candidates_per_repository(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    await make_index(env, fixture, status="queued")
    with pytest.raises(IntegrityError):
        await make_index(env, fixture, status="building")


async def test_import_composition_supersedes_older_unfinished_candidate(
    knowledge_env, chunker
):
    env = knowledge_env
    older = await ready_import(env, chunk_count=2)
    stale = await builder(env, index=FakeMilvus()).request(
        env.actor, older.import_id, "older-build"
    )

    receipt, _revision_id, _ = await upload(env, b"auto supersede body\n" * 20)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)

    async with env.factory() as db:
        old = await db.get(IndexVersion, stale.index_id)
        old_job = await db.get(Job, old.job_id)
        unfinished = list(
            await db.scalars(
                select(IndexVersion).where(
                    IndexVersion.repository_id == env.repository.id,
                    IndexVersion.status.in_(("queued", "building")),
                )
            )
        )
    assert old.status == "superseded"
    assert old.queryable is False
    assert old_job.status == "succeeded"
    assert old_job.result == {"status": "superseded"}
    assert len(unfinished) == 1
    auto = unfinished[0]
    assert auto.idempotency_key.startswith("auto-")
    assert auto.status == "queued"
    assert auto.target_generation == receipt.target_generation


async def test_stale_executor_leaves_recovered_candidate_untouched(
    knowledge_env,
):
    env = knowledge_env
    fixture, _fake, build, receipt, lease, _proof = await build_to_pending(env)

    # A newer owner recovered the same batch Job, and the import is no longer
    # publishable. The stale executor must not terminalize the candidate that
    # the recovered owner is still responsible for.
    async with env.factory.begin() as db:
        job = await db.get(Job, lease.job_id)
        job.lease_owner = uuid4()
        job.attempt += 1
        revision = await db.get(DocumentRevision, fixture.revision_ids[0])
        revision.searchable = False

    published = await build.publish(
        lease, receipt.index_id, await proof_for(env, receipt.index_id)
    )

    assert published is False
    row, job, corpus = await index_state(env, receipt.index_id)
    assert row.status == "building"
    assert row.queryable is False
    assert job.status == "running"
    assert job.attempt == lease.attempt + 1
    assert corpus.active_index_version_id is None


async def test_zero_member_import_publishes_empty_index(knowledge_env):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=0)
    fake = FakeMilvus()
    build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=fake)
    row, _, _ = await index_state(env, receipt.index_id)
    assert row.expected_member_count == 0
    assert row.member_digest == sha256(b"").hexdigest()

    await build.run_batch(lease)
    row, job, corpus = await index_state(env, receipt.index_id)
    assert (row.status, row.queryable) == ("ready", True)
    assert corpus.active_index_version_id == row.id
    assert job.status == "succeeded"
    assert fake.upserts == []


async def test_import_completion_requests_same_generation_index(knowledge_env, chunker):
    env = knowledge_env
    receipt, revision_id, _ = await upload(env, b"auto index body\n" * 20)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)

    async with env.factory() as db:
        imported = await db.get(KnowledgeImport, receipt.import_id)
        row = await db.scalar(
            select(IndexVersion).where(IndexVersion.import_id == receipt.import_id)
        )
        assert row is not None
        job = await db.get(Job, row.job_id)
        members = list(
            await db.scalars(
                select(IndexMember)
                .where(IndexMember.index_version_id == row.id)
                .order_by(IndexMember.ordinal)
            )
        )
        chunks = set(
            await db.scalars(select(Chunk.id).where(Chunk.revision_id == revision_id))
        )
        imported_members = set(
            await db.scalars(
                select(ImportMember.revision_id).where(
                    ImportMember.import_id == receipt.import_id
                )
            )
        )
    assert imported_members == {revision_id}
    assert row.target_generation == receipt.target_generation
    assert row.target_generation == imported.target_generation
    assert row.target_sha == imported.target_sha
    assert row.status == "queued"
    assert row.expected_member_count == len(members) > 0
    assert row.member_digest == digest(
        (member.chunk_id, member.ordinal) for member in members
    )
    assert job.kind == "knowledge.index_batch"
    assert job.status == "queued"
    assert job.requested_by == env.actor.id
    assert job.input["index_id"] == str(row.id)
    assert {member.chunk_id for member in members} == chunks


@pytest.mark.skipif(
    not MILVUS_URI,
    reason="TEST_MILVUS_URI is not set; real Milvus publication NOT EXECUTED",
)
async def test_real_milvus_partial_write_stays_unpublished_until_visible(
    knowledge_env,
):
    """Replay one partial-write case against the real Task 3 service.

    The deterministic fakes cover racing failure paths; this case proves the
    builder's Strong-visibility gate against actual Milvus rows.
    """
    from pymilvus import MilvusClient

    env = knowledge_env
    collection = f"devflow_publication_{uuid4().hex[:20]}"
    settings = Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        milvus_uri=MILVUS_URI,
        milvus_collection=collection,
        milvus_timeout_seconds=5,
        milvus_build_timeout_seconds=120,
    )
    client = MilvusClient(uri=MILVUS_URI, timeout=5)
    real = MilvusIndex(settings, client=client)
    try:
        fixture = await ready_import(env, chunk_count=32)
        build, receipt, lease = await request_and_claim(env, fixture=fixture, fake=real)
        await build.run_batch(lease)

        row, job, corpus = await index_state(env, receipt.index_id)
        assert row.status == "building"
        assert row.written_count == 16
        assert corpus.active_index_version_id is None
        async with env.factory() as db:
            written = list(
                await db.scalars(
                    select(IndexMember.chunk_id).where(
                        IndexMember.index_version_id == receipt.index_id,
                        IndexMember.status == "written",
                    )
                )
            )
            manifest = list(
                await db.scalars(
                    select(IndexMember.chunk_id).where(
                        IndexMember.index_version_id == receipt.index_id
                    )
                )
            )
        assert len(written) == 16
        assert real.verify_visible(receipt.index_id, written) is True
        assert real.verify_visible(receipt.index_id, manifest) is False

        next_lease = await Queue(env.factory, lease_seconds=300).claim(
            uuid4(), allowed_kinds=MODEL_JOB_KINDS
        )
        assert next_lease is not None
        await build.run_batch(next_lease)
        row, job, corpus = await index_state(env, receipt.index_id)
        assert (row.status, row.queryable) == ("ready", True)
        assert corpus.active_index_version_id == row.id
        assert job.status == "succeeded"
        assert real.verify_visible(row.id, manifest) is True
    finally:
        try:
            client.drop_collection(collection)
        finally:
            client.close()
