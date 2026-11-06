"""Bounded reclamation, owner protection and the periodic cleanup producer."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_knowledge_chunking import chunker as chunker  # noqa: PLC0414

from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import KnowledgeCleanupInput
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    KnowledgeCapacity,
    RepositoryCapacity,
    RevisionPin,
    RevisionRetention,
)
from devflow.knowledge.retention import (
    KnowledgeMaintenance,
    KnowledgeRetention,
)
from devflow.knowledge.schemas import SourceIdentity, UploadImportRequest
from devflow.knowledge.service import KnowledgeService
from devflow.knowledge.sources import SourceReader

pytestmark = pytest.mark.asyncio


def identity_for(env, revision, document):
    return SourceIdentity(
        repository_id=env.repository.id,
        source_repository_id=env.repository.id,
        dependency_repository_ids=(env.repository.id,),
        document_id=document.id,
        revision_hash=revision.revision_hash,
        workspace_id=None,
        sha=None,
        path=None,
    )


async def upload(env, body, *, document_id=None):
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(
        env.actor,
        env.repository.id,
        "note.md",
        body,
        document_id=document_id,
    )
    receipt = await service.request_import(
        env.actor,
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            document_id=document_id,
            filename="note.md",
            staging_id=stage,
        ),
        uuid4().hex,
    )
    async with env.factory() as db:
        revision = await db.scalar(
            select(DocumentRevision)
            .join(
                ImportMember,
                ImportMember.revision_id == DocumentRevision.id,
            )
            .where(ImportMember.import_id == receipt.import_id)
        )
        document = await db.get(Document, revision.document_id)
        return receipt, revision.id, document.id


async def finish(env, service, receipt):
    from devflow.knowledge.models import KnowledgeImport

    job_id = receipt.job_id
    for _ in range(150):
        await env.due(job_id)
        lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
        assert lease is not None and lease.job_id == job_id
        await service.import_batch(lease)
        async with env.factory() as db:
            current = await db.get(KnowledgeImport, receipt.import_id)
            if current.status in {"ready", "ready_partial"}:
                return current
            job_id = await db.scalar(
                select(Job.id).where(
                    Job.kind == "knowledge.import",
                    Job.status == "queued",
                    Job.input["import_id"].astext == str(receipt.import_id),
                )
            )
            assert job_id is not None
    pytest.fail("bounded import did not finish")


async def cleanup_job(env, *, after=None, limit=100):
    value = KnowledgeCleanupInput(version=1, limit=limit, after=after).model_dump(
        mode="json"
    )
    async with env.factory.begin() as db:
        job = Job(
            kind="knowledge.cleanup",
            repository_id=env.repository.id,
            requested_by=None,
            input=value,
            input_hash=hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            idempotency_key=uuid4().hex,
        )
        db.add(job)
        await db.flush()
        return job.id


async def run_cleanup(env, *, after=None, limit=100):
    job_id = await cleanup_job(env, after=after, limit=limit)
    await env.due(job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert lease is not None and lease.job_id == job_id
    result = await KnowledgeRetention(env.factory).sweep(lease)
    return job_id, result


async def direct_revisions(env, count, body=b"x"):
    ids = []
    async with env.factory.begin() as db:
        await db.execute(
            pg_insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
        )
        await db.execute(
            pg_insert(RepositoryCapacity)
            .values(repository_id=env.repository.id)
            .on_conflict_do_nothing()
        )
        capacity = await db.get(KnowledgeCapacity, 1, with_for_update=True)
        for index in range(count):
            document = Document(
                repository_id=env.repository.id,
                source_repository_id=env.repository.id,
                kind="upload",
                attributes={"filename": f"note-{index}.md"},
            )
            db.add(document)
            await db.flush()
            digest = hashlib.sha256(body + str(index).encode()).hexdigest()
            revision = DocumentRevision(
                document_id=document.id,
                revision_hash=digest,
                source_version=digest,
                body=body,
                body_bytes=len(body),
            )
            db.add(revision)
            await db.flush()
            ids.append(revision.id)
        capacity.body_bytes += count * len(body)
    return ids


async def test_delete_beats_retention_and_pin(knowledge_env, chunker):
    env = knowledge_env
    body = b"A" * 200
    receipt, revision_id, document_id = await upload(env, body)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)

    reader = SourceReader(env.factory, env.policy)
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        document = await db.get(Document, document_id)
        identity = identity_for(env, revision, document)
        assert revision.body == body
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            RevisionPin(
                revision_id=revision_id,
                expires_at=now + timedelta(minutes=5),
            )
        )
        db.add(
            RevisionRetention(
                owner_kind="query",
                owner_id=uuid4(),
                revision_id=revision_id,
                expires_at=now + timedelta(minutes=5),
            )
        )
    assert (await reader.preview(env.actor, identity)).text.encode() == body

    deletion = await service.request_import(
        env.actor,
        {
            "kind": "delete",
            "repository_id": env.repository.id,
            "document_id": document_id,
        },
        uuid4().hex,
    )
    await finish(env, service, deletion)

    view = await reader.preview(env.actor, identity)
    assert view.reason == "source_unavailable"
    assert view.text is None and view.next_cursor is None and not view.partial
    with pytest.raises(AppError) as denied:
        await reader.read(env.actor, identity)
    assert denied.value.code == "resource_not_found"

    _, protected = await run_cleanup(env)
    assert protected.examined == 1
    assert protected.reclaimed == 0 and protected.deferred == 1
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        assert revision.body == body
        retained = await db.scalar(
            select(RevisionRetention).where(
                RevisionRetention.revision_id == revision_id
            )
        )
        pinned = await db.scalar(
            select(RevisionPin).where(RevisionPin.revision_id == revision_id)
        )
        before_bytes = (await db.get(KnowledgeCapacity, 1)).body_bytes
        assert retained is not None and pinned is not None

    async with env.factory.begin() as db:
        await db.execute(
            delete(RevisionRetention).where(
                RevisionRetention.revision_id == revision_id
            )
        )
        await db.execute(
            delete(RevisionPin).where(RevisionPin.revision_id == revision_id)
        )
    _, reclaimed = await run_cleanup(env)
    assert reclaimed.examined == 1
    assert reclaimed.reclaimed == 1 and reclaimed.deferred == 0
    assert reclaimed.bytes_reclaimed == len(body)
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        assert revision.body is None
        assert revision.body_bytes == len(body)
        assert revision.availability == "deleted"
        assert not revision.searchable
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.revision_id == revision_id)
            )
            == 0
        )
        assert (await db.get(KnowledgeCapacity, 1)).body_bytes == before_bytes - len(
            body
        )
        document = await db.get(Document, document_id)
        assert document.deleted and document.accepted_revision_id is None


async def test_retained_revision_not_deleted_for_capacity(knowledge_env, chunker):
    env = knowledge_env
    first_body = b"A" * 100
    receipt, first_id, document_id = await upload(env, first_body)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)

    second_body = b"B" * 120
    receipt, second_id, _ = await upload(env, second_body, document_id=document_id)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)
    async with env.factory() as db:
        new = await db.get(DocumentRevision, second_id)
        document = await db.get(Document, document_id)
        assert document.accepted_revision_id == second_id
        assert new.body == second_body
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        db.add(
            RevisionRetention(
                owner_kind="message",
                owner_id=uuid4(),
                revision_id=first_id,
                expires_at=now + timedelta(hours=1),
            )
        )

    _, retained = await run_cleanup(env)
    assert retained.reclaimed == 0 and retained.deferred >= 1
    async with env.factory() as db:
        assert (await db.get(DocumentRevision, first_id)).body == first_body

    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        row = await db.scalar(
            select(RevisionRetention).where(RevisionRetention.revision_id == first_id)
        )
        row.expires_at = now - timedelta(seconds=1)

    _, reclaimed = await run_cleanup(env)
    assert reclaimed.reclaimed == 1
    async with env.factory() as db:
        old = await db.get(DocumentRevision, first_id)
        new = await db.get(DocumentRevision, second_id)
        assert old.body is None and old.availability == "expired"
        assert old.revision_hash == hashlib.sha256(first_body).hexdigest()
        assert old.body_bytes == len(first_body)
        assert new.body == second_body and new.availability == "available"
        assert (await db.get(Document, document_id)).accepted_revision_id == second_id

    async with env.factory.begin() as db:
        capacity = await db.get(KnowledgeCapacity, 1, with_for_update=True)
        capacity.body_bytes = 134217728
    with pytest.raises(AppError) as exhausted:
        await KnowledgeService(env.factory, env.policy).stage_upload(
            env.actor, env.repository.id, "note.md", b"one more"
        )
    assert exhausted.value.code == "storage_exhausted"


async def test_cleanup_is_bounded_and_advances_past_protected_rows(knowledge_env):
    env = knowledge_env
    ids = sorted(await direct_revisions(env, 5))
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        for revision_id in ids[:3]:
            db.add(
                RevisionPin(
                    revision_id=revision_id,
                    expires_at=now + timedelta(minutes=5),
                )
            )

    _, first = await run_cleanup(env, limit=3)
    assert first.examined == 3 and first.deferred == 3 and first.reclaimed == 0
    assert first.cursor == ids[2]

    _, second = await run_cleanup(env, after=first.cursor, limit=3)
    assert second.examined == 2 and second.reclaimed == 2
    assert second.cursor == ids[4]

    async with env.factory.begin() as db:
        await db.execute(delete(RevisionPin))
    _, wrapped = await run_cleanup(env, after=second.cursor, limit=3)
    assert wrapped.examined == 3 and wrapped.reclaimed == 3
    assert wrapped.wrapped is True
    assert wrapped.cursor == ids[2]

    _, exhausted = await run_cleanup(env, after=wrapped.cursor, limit=3)
    assert exhausted.examined == 0
    assert exhausted.wrapped is True and exhausted.cursor is None
    assert exhausted.reclaimed == 0


async def test_maintenance_enqueues_bounded_batches_and_coalesces(knowledge_env):
    env = knowledge_env
    ids = sorted(await direct_revisions(env, 101))
    maintenance = KnowledgeMaintenance(env.factory)
    assert await maintenance.tick() == 1
    # A fresh producer instance has no process memory; coalescing is DB-only.
    assert await KnowledgeMaintenance(env.factory).tick() == 0

    async with env.factory() as db:
        first = await db.scalar(
            select(Job).where(
                Job.kind == "knowledge.cleanup",
                Job.repository_id == env.repository.id,
            )
        )
        assert first.requested_by is None
        assert first.input == {"version": 1, "limit": 100, "after": None}
    await env.due(first.id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert lease is not None and lease.job_id == first.id
    result = await KnowledgeRetention(env.factory).sweep(lease)
    assert result.reclaimed == 100 and result.examined == 100
    cursor = result.cursor
    assert cursor == ids[99]

    assert await KnowledgeMaintenance(env.factory).tick() == 0
    async with env.factory.begin() as db:
        first = await db.get(Job, first.id)
        now = await db.scalar(select(func.clock_timestamp()))
        first.updated_at = now - timedelta(minutes=2)
    assert await KnowledgeMaintenance(env.factory).tick() == 1
    async with env.factory() as db:
        second = await db.scalar(
            select(Job)
            .where(
                Job.kind == "knowledge.cleanup",
                Job.id != first.id,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        assert second.input["after"] == str(cursor)
    await env.due(second.id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert lease is not None and lease.job_id == second.id
    final = await KnowledgeRetention(env.factory).sweep(lease)
    assert final.reclaimed == 1


async def test_terminal_failure_cooldown_creates_a_new_batch(knowledge_env):
    env = knowledge_env
    await direct_revisions(env, 1)
    maintenance = KnowledgeMaintenance(env.factory)
    assert await maintenance.tick() == 1
    async with env.factory() as db:
        job = await db.scalar(select(Job).where(Job.kind == "knowledge.cleanup"))
    await env.due(job.id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert await Queue(env.factory).fail(lease, "internal_error", False)
    assert await maintenance.tick() == 0

    async with env.factory.begin() as db:
        failed = await db.get(Job, job.id)
        assert failed.status == "failed"
        now = await db.scalar(select(func.clock_timestamp()))
        failed.updated_at = now - timedelta(hours=2)
    assert await maintenance.tick() == 1
    async with env.factory() as db:
        replacement = await db.scalar(
            select(Job).where(Job.kind == "knowledge.cleanup", Job.id != job.id)
        )
        assert replacement.status == "queued"
        assert replacement.idempotency_key != job.idempotency_key


async def test_revoked_repository_is_still_cleaned(knowledge_env):
    env = knowledge_env
    await direct_revisions(env, 1)
    env.repositories[(11, 1)] = ([], None)
    maintenance = KnowledgeMaintenance(env.factory)
    assert await maintenance.tick() == 1
    async with env.factory() as db:
        job = await db.scalar(select(Job).where(Job.kind == "knowledge.cleanup"))
    await env.due(job.id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    result = await KnowledgeRetention(env.factory).sweep(lease)
    assert result.reclaimed == 1
    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(DocumentRevision)
                .where(DocumentRevision.body.is_not(None))
            )
            == 0
        )


async def test_retain_sources_is_locked_bounded_and_never_resurrects(knowledge_env):
    from devflow.knowledge.retention import retain_sources

    env = knowledge_env
    ids = await direct_revisions(env, 1)
    async with env.factory() as db:
        now = await db.scalar(select(func.clock_timestamp()))
    owner = uuid4()

    async with env.factory.begin() as db:
        await retain_sources(db, "query", owner, tuple(ids), now + timedelta(minutes=5))
    _, protected = await run_cleanup(env)
    assert protected.reclaimed == 0 and protected.deferred == 1

    later = now + timedelta(hours=1)
    async with env.factory.begin() as db:
        await retain_sources(db, "query", owner, tuple(ids), later)
    async with env.factory() as db:
        row = await db.scalar(
            select(RevisionRetention).where(
                RevisionRetention.owner_id == owner,
                RevisionRetention.revision_id == ids[0],
            )
        )
        assert row.expires_at == later

    with pytest.raises(AppError):
        async with env.factory.begin() as db:
            await retain_sources(
                db, "invalid", owner, tuple(ids), now + timedelta(minutes=1)
            )
    with pytest.raises(AppError):
        async with env.factory.begin() as db:
            await retain_sources(
                db,
                "message",
                owner,
                tuple(uuid4() for _ in range(101)),
                now + timedelta(minutes=1),
            )

    async with env.factory.begin() as db:
        row = await db.scalar(
            select(RevisionRetention).where(
                RevisionRetention.owner_id == owner,
                RevisionRetention.revision_id == ids[0],
            )
        )
        row.expires_at = now - timedelta(days=1)
    _, reclaimed = await run_cleanup(env)
    assert reclaimed.reclaimed == 1

    other = uuid4()
    async with env.factory.begin() as db:
        await retain_sources(db, "message", other, tuple(ids), now + timedelta(hours=1))
    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RevisionRetention)
                .where(RevisionRetention.owner_id == other)
            )
            == 0
        )


async def test_narrowed_workspace_scope_reclaims_dropped_path(knowledge_env):
    from devflow.knowledge.models import (
        KnowledgeCorpus,
        RevisionDependency,
    )
    from devflow.knowledge.sources import workspace_source_version

    env = knowledge_env
    version = await env.workspace()
    async with env.factory.begin() as db:
        db.add(
            KnowledgeCorpus(
                repository_id=env.repository.id,
                target_generation=2,
                target_workspace_id=version.id,
                target_sha=version.commit_sha,
                path_scopes={
                    "document_paths": ["docs/keep.md"],
                    "code_paths": [],
                },
            )
        )
        await db.execute(
            pg_insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
        )
        await db.execute(
            pg_insert(RepositoryCapacity)
            .values(repository_id=env.repository.id)
            .on_conflict_do_nothing()
        )
        capacity = await db.get(KnowledgeCapacity, 1, with_for_update=True)
        revisions = {}
        for path, body in (
            ("docs/keep.md", b"keep"),
            ("docs/drop.md", b"drop"),
        ):
            document = Document(
                repository_id=env.repository.id,
                source_repository_id=env.repository.id,
                kind="workspace",
                path=path,
            )
            db.add(document)
            await db.flush()
            digest = hashlib.sha256(body).hexdigest()
            revision = DocumentRevision(
                document_id=document.id,
                revision_hash=digest,
                source_version=workspace_source_version(
                    repository_id=env.repository.id,
                    source_repository_id=env.repository.id,
                    dependency_repository_ids=(env.repository.id,),
                    workspace_id=version.id,
                    sha=version.commit_sha,
                    path=path,
                ),
                workspace_id=version.id,
                sha=version.commit_sha,
                body=body,
                body_bytes=len(body),
            )
            db.add(revision)
            await db.flush()
            db.add(
                RevisionDependency(
                    revision_id=revision.id, repository_id=env.repository.id
                )
            )
            revisions[path] = revision.id
        capacity.body_bytes += 9

    maintenance = KnowledgeMaintenance(env.factory)
    assert await maintenance.tick() == 1
    async with env.factory() as db:
        job = await db.scalar(select(Job).where(Job.kind == "knowledge.cleanup"))
    await env.due(job.id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    result = await KnowledgeRetention(env.factory).sweep(lease)
    assert result.examined == 2
    assert result.reclaimed == 1 and result.deferred == 1
    async with env.factory() as db:
        kept = await db.get(DocumentRevision, revisions["docs/keep.md"])
        dropped = await db.get(DocumentRevision, revisions["docs/drop.md"])
        assert kept.body == b"keep" and kept.availability == "available"
        assert dropped.body is None and dropped.availability == "expired"


async def test_restaging_identical_upload_restores_reclaimed_revision(
    knowledge_env, chunker
):
    env = knowledge_env
    body = b"same upload bytes\n"
    receipt, first_id, document_id = await upload(env, body)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)

    receipt, _, _ = await upload(env, b"replacement\n", document_id=document_id)
    await finish(env, service, receipt)
    _, reclaimed = await run_cleanup(env)
    assert reclaimed.reclaimed == 1
    async with env.factory() as db:
        old = await db.get(DocumentRevision, first_id)
        assert old.body is None and old.availability == "expired"
        before = (await db.get(KnowledgeCapacity, 1)).body_bytes

    final_receipt, restored_id, _ = await upload(env, body, document_id=document_id)
    assert restored_id == first_id
    async with env.factory() as db:
        restored = await db.get(DocumentRevision, first_id)
        assert restored.body == body and restored.availability == "available"
        assert restored.searchable is False
        assert (await db.get(KnowledgeCapacity, 1)).body_bytes == before + len(body)
    await finish(env, service, final_receipt)
    texts = [
        chunk.text async for chunk in service.completed_chunks(final_receipt.import_id)
    ]
    assert any("same upload bytes" in text for text in texts)
    async with env.factory() as db:
        assert (await db.get(DocumentRevision, first_id)).availability == "available"
