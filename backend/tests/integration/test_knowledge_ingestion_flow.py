"""End-to-end 3A delivery: upload, replacement, Workspace import, revocation."""

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_knowledge_chunking import chunker as chunker  # noqa: PLC0414
from test_workspace_reads import MemoryCache

from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import KnowledgeCleanupInput
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    ImportMember,
    KnowledgeImport,
)
from devflow.knowledge.retention import KnowledgeRetention
from devflow.knowledge.schemas import (
    SourceIdentity,
    UploadImportRequest,
    WorkspaceImportRequest,
)
from devflow.knowledge.service import KnowledgeService
from devflow.knowledge.sources import SourceReader
from devflow.workspace.read import WorkspaceReader

pytestmark = pytest.mark.asyncio


class Cache(MemoryCache):
    def blob_size(self, repo, oid):
        return len(self.blobs[oid])


async def run_import(env, service, receipt):
    job_id = receipt.job_id
    for _ in range(200):
        await env.due(job_id)
        lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
        assert lease is not None and lease.job_id == job_id
        await service.import_batch(lease)
        async with env.factory() as db:
            row = await db.get(KnowledgeImport, receipt.import_id)
            if row.status in {"ready", "ready_partial"}:
                return row
            job_id = await db.scalar(
                select(Job.id).where(
                    Job.kind == "knowledge.import",
                    Job.status == "queued",
                    Job.input["import_id"].astext == str(receipt.import_id),
                )
            )
            assert job_id is not None
    pytest.fail("bounded import did not finish")


async def stage_and_import(env, service, body, *, document_id=None):
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
    await run_import(env, service, receipt)
    async with env.factory() as db:
        member = await db.scalar(
            select(ImportMember).where(ImportMember.import_id == receipt.import_id)
        )
        revision = await db.get(DocumentRevision, member.revision_id)
        document = await db.get(Document, revision.document_id)
        return revision, document


async def test_upload_replacement_workspace_partial_and_revocation(
    knowledge_env, chunker, workspace_tmp_path
):
    env = knowledge_env
    first_body = b"first upload body\n"
    replacement_body = b"replacement body\n"
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)

    first, document = await stage_and_import(env, service, first_body)
    replacement, _ = await stage_and_import(
        env, service, replacement_body, document_id=document.id
    )
    async with env.factory() as db:
        document = await db.get(Document, document.id)
        assert document.accepted_revision_id == replacement.id
        identity = SourceIdentity(
            repository_id=env.repository.id,
            source_repository_id=env.repository.id,
            dependency_repository_ids=(env.repository.id,),
            document_id=document.id,
            revision_hash=first.revision_hash,
            workspace_id=None,
            sha=None,
            path=None,
        )

    cache = Cache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"# A\nworkspace body\n")
    cache.add(b"docs/b.md", b"# B\nsecond file\n")
    cache.add(b"src/c.py", b"print('c')\n")
    cache.add(b"docs/secret.pem", b"excluded credential")
    version = await env.workspace()
    reader = SourceReader(
        env.factory,
        env.policy,
        WorkspaceReader(env.factory, env.policy, cache),
    )
    service = KnowledgeService(env.factory, env.policy, chunker=chunker, reader=reader)
    receipt = await service.request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace",
            repository_id=env.repository.id,
            workspace_id=version.id,
            code_paths=("src",),
        ),
        uuid4().hex,
    )
    imported = await run_import(env, service, receipt)
    assert imported.status == "ready_partial"

    async with env.factory() as db:
        members = list(
            await db.scalars(
                select(ImportMember).where(ImportMember.import_id == imported.id)
            )
        )
        included = {
            member.source_key for member in members if member.status == "included"
        }
        assert "docs/a.md" in included
        assert "docs/b.md" in included
        assert "src/c.py" in included
        assert "docs/secret.pem" not in included
        assert (
            await db.scalar(
                select(ImportMember.id).where(
                    ImportMember.import_id == imported.id,
                    ImportMember.source_key == "docs/secret.pem",
                    ImportMember.status == "skipped",
                )
            )
            is not None
        )

    view = await reader.preview(env.actor, identity)
    assert view.text.encode() == first_body
    assert view.is_current is False
    texts = [chunk.text async for chunk in service.completed_chunks(imported.id)]
    assert any("workspace body" in text for text in texts)
    assert any("replacement body" in text for text in texts)
    assert not any("first upload body" in text for text in texts)

    env.repositories[(11, 1)] = ([], None)
    from devflow.repositories.models import Repository

    async with env.factory.begin() as db:
        repository = await db.get(Repository, env.repository.id)
        repository.auth_version += 1
    with pytest.raises(AppError):
        await reader.preview(env.actor, identity)


async def cleanup_once(env):
    value = KnowledgeCleanupInput(version=1, limit=100, after=None).model_dump(
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
        job_id = job.id
    await env.due(job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(uuid4())
    assert lease is not None and lease.job_id == job_id
    return await KnowledgeRetention(env.factory).sweep(lease)


async def test_workspace_reimport_restores_reclaimed_revision(
    knowledge_env, chunker, workspace_tmp_path
):
    env = knowledge_env
    body = b"# Re-imported workspace bytes\n"
    cache = Cache(workspace_tmp_path)
    cache.add(b"docs/a.md", body)
    version = await env.workspace()
    reader = SourceReader(
        env.factory,
        env.policy,
        WorkspaceReader(env.factory, env.policy, cache),
    )
    service = KnowledgeService(env.factory, env.policy, chunker=chunker, reader=reader)

    async def admit(document_paths):
        receipt = await service.request_import(
            env.actor,
            WorkspaceImportRequest(
                kind="workspace",
                repository_id=env.repository.id,
                workspace_id=version.id,
                document_paths=document_paths,
            ),
            uuid4().hex,
        )
        return receipt, await run_import(env, service, receipt)

    first_receipt, _ = await admit(("docs/a.md",))
    async with env.factory() as db:
        revision_id = await db.scalar(
            select(ImportMember.revision_id).where(
                ImportMember.import_id == first_receipt.import_id,
                ImportMember.source_key == "docs/a.md",
            )
        )
        assert revision_id is not None

    # A narrower import advances the generation; docs/a.md is no longer current.
    await admit(("README",))
    async with env.factory() as db:
        # Searchability belongs to the active manifest: once the new generation
        # drops the revision, it must stop advertising itself immediately rather
        # than waiting for reclamation to expire it.
        assert (await db.get(DocumentRevision, revision_id)).searchable is False
    reclaimed = await cleanup_once(env)
    assert reclaimed.reclaimed >= 1
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        assert revision.body is None
        assert revision.availability == "expired"

    third_receipt, third = await admit(("docs/a.md",))
    async with env.factory() as db:
        revision = await db.get(DocumentRevision, revision_id)
        assert revision.body == body
        assert revision.availability == "available"
    assert third.status in {"ready", "ready_partial"}
    texts = [
        chunk.text async for chunk in service.completed_chunks(third_receipt.import_id)
    ]
    assert any("Re-imported workspace bytes" in text for text in texts)
