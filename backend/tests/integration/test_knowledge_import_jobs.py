"""Durable corpus imports over migrated dedicated PG and real policy/queue."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_knowledge_chunking import chunker as chunker  # noqa: PLC0414
from test_workspace_reads import MemoryCache

from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    ImportMember,
    KnowledgeImport,
)
from devflow.knowledge.schemas import UploadImportRequest
from devflow.knowledge.service import KnowledgeService


async def upload(env, body=b"hello", document_id=None):
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(
        env.actor, env.repository.id, "note.md", body, document_id=document_id
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
            .join(ImportMember, ImportMember.revision_id == DocumentRevision.id)
            .where(ImportMember.import_id == receipt.import_id)
        )
        return receipt, revision.document_id


async def claim(env, job_id=None):
    if job_id:
        await env.due(job_id)
    queue = Queue(env.factory, lease_seconds=300)
    lease = await queue.claim(uuid4())
    assert lease is not None, "real knowledge handler kind must be claimable"
    return lease


async def finish(env, service, receipt):
    job_id = receipt.job_id
    for _ in range(150):
        lease = await claim(env, job_id)
        result = await service.import_batch(lease)
        async with env.factory() as db:
            current = await db.get(KnowledgeImport, receipt.import_id)
            if current.status in {"ready", "ready_partial"}:
                return result
            job_id = await db.scalar(
                select(Job.id).where(
                    Job.kind == "knowledge.import",
                    Job.status == "queued",
                    Job.input["import_id"].astext == str(receipt.import_id),
                )
            )
            assert job_id is not None
    pytest.fail("bounded import did not finish")


@pytest.mark.asyncio
async def test_accepted_upload_selection_survives_pending_successor(knowledge_env):
    env = knowledge_env
    first, document = await upload(env, b"A")
    second, other = await upload(env, b"B")
    async with env.factory() as db:
        a = await db.get(Document, document)
        b = await db.get(Document, other)
        assert getattr(a, "accepted_revision_id", None) is not None
        assert getattr(b, "accepted_revision_id", None) is not None
        target = await db.get(KnowledgeImport, second.import_id)
        assert target.target_generation == 2 and target.composition_version == 1
        assert (await db.get(KnowledgeImport, first.import_id)).job_id == first.job_id


@pytest.mark.asyncio
async def test_import_batch_resume_keeps_deadline_and_limits(knowledge_env, chunker):
    from devflow.knowledge.models import Chunk

    env = knowledge_env
    receipt, _ = await upload(env, b"abcdefghij " * 1500)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    async with env.factory() as db:
        before = await db.get(KnowledgeImport, receipt.import_id)
        deadline = before.deadline
    lease = await claim(env, receipt.job_id)
    await service.import_batch(lease)
    async with env.factory() as db:
        assert 0 < await db.scalar(select(func.count()).select_from(Chunk)) <= 16
        row = await db.get(KnowledgeImport, receipt.import_id)
        assert row.deadline == deadline and row.failures == 0
        next_job = await db.scalar(
            select(Job.id).where(Job.status == "queued", Job.kind == "knowledge.import")
        )
    assert next_job is not None
    # Reinstantiate the service: no in-memory drafts/cursor may be required.
    from types import SimpleNamespace

    await finish(
        env,
        KnowledgeService(env.factory, env.policy, chunker=chunker),
        SimpleNamespace(job_id=next_job, import_id=receipt.import_id),
    )
    chunks = [item async for item in service.completed_chunks(receipt.import_id)]
    assert len(chunks) > 16 and len({c.id for c in chunks}) == len(chunks)
    async with env.factory() as db:
        row = await db.get(KnowledgeImport, receipt.import_id)
        assert row.deadline == deadline and row.job_id == receipt.job_id


@pytest.mark.asyncio
async def test_full_corpus_and_delete_replay(knowledge_env, chunker):
    env = knowledge_env
    first, a = await upload(env, b"A")
    second, _ = await upload(env, b"B")
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    # The older accepted mutation must not be necessary to publish A.
    async with env.factory.begin() as db:
        (await db.get(Job, first.job_id)).status = "failed"
    await finish(env, service, second)
    assert {
        c.text for c in [c async for c in service.completed_chunks(second.import_id)]
    } == {"A", "B"}
    request = {"kind": "delete", "repository_id": env.repository.id, "document_id": a}
    deletion = await service.request_import(env.actor, request, "delete-A")
    assert await service.request_import(env.actor, request, "delete-A") == deletion
    async with env.factory() as db:
        document = await db.get(Document, a)
        assert document.deleted and document.accepted_revision_id is None
    await finish(env, service, deletion)
    assert [c.text async for c in service.completed_chunks(deletion.import_id)] == ["B"]
    from devflow.errors import AppError

    with pytest.raises(AppError):
        await service.request_import(env.actor, request, "delete-again")


@pytest.mark.asyncio
async def test_stale_import_cannot_publish(knowledge_env, chunker):
    from dataclasses import replace

    from devflow.knowledge.models import Chunk

    env = knowledge_env
    receipt, _ = await upload(env)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    lease = await claim(env, receipt.job_id)
    for wrong in (
        replace(lease, owner=uuid4()),
        replace(lease, generation=lease.generation + 1),
        replace(lease, attempt=lease.attempt + 1),
    ):
        await service.import_batch(wrong)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 0
    await upload(env, b"new generation")
    await service.import_batch(lease)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 0


@pytest.mark.asyncio
async def test_failure_budget_and_recovery_are_cumulative(knowledge_env, chunker):
    env = knowledge_env
    receipt, _ = await upload(env)
    queue = Queue(
        env.factory, lease_seconds=300, retry_first_seconds=0, retry_second_seconds=0
    )
    for expected in (1, 2, 3):
        lease = await claim(env, receipt.job_id)
        assert await queue.fail(lease, "attempt_timeout", True)
        assert not await queue.fail(lease, "attempt_timeout", True)
        async with env.factory() as db:
            row = await db.get(KnowledgeImport, receipt.import_id)
            assert row.failures == expected
            if expected == 3:
                assert row.status == "failed"


@pytest.mark.asyncio
async def test_composed_upload_requires_selected_fork_authority(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.schemas import WorkspaceImportRequest

    env = knowledge_env
    fork = await env.fork()
    version = await env.workspace(fork)
    service = KnowledgeService(env.factory, env.policy)
    await service.request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace", repository_id=env.repository.id, workspace_id=version.id
        ),
        "workspace",
    )
    env.repositories[(12, 1)] = ([], None)
    with pytest.raises(AppError):
        await upload(env)


@pytest.mark.asyncio
async def test_metadata_page_has_size_and_explicit_truncation(
    knowledge_env, workspace_tmp_path
):
    from test_workspace_reads import MemoryCache

    from devflow.workspace.read import WorkspaceReader

    class Cache(MemoryCache):
        def blob_size(self, repo, oid):
            return len(self.blobs[oid])

    env = knowledge_env
    version = await env.workspace()
    cache = Cache(workspace_tmp_path)
    cache.add(b"docs/z.md", b"last")
    cache.add(b"docs/a.md", b"first")
    reader = WorkspaceReader(env.factory, env.policy, cache)
    page = await reader.metadata(
        env.actor,
        env.repository.id,
        version.id,
        sha=version.commit_sha,
        cursor=0,
        limit=1,
    )
    assert page.items[0].path == "docs/a.md" and page.items[0].size == 5
    assert page.next_cursor == 1 and not page.exhausted and not page.truncated
    cache.truncated = True
    page = await reader.metadata(
        env.actor,
        env.repository.id,
        version.id,
        sha=version.commit_sha,
        cursor=1,
        limit=1,
    )
    assert page.items[0].path == "docs/z.md"
    assert page.next_cursor is None and page.truncated and not page.exhausted
    assert not any(call[0] == "blob" for call in cache.calls)


class ImportCache(MemoryCache):
    def blob_size(self, repo, oid):
        return len(self.blobs[oid])

    def blob(self, repo, oid, max_bytes):
        from devflow.workspace.process import WorkspaceProcessError

        self.calls.append(("blob", repo, oid, max_bytes))
        if len(self.blobs[oid]) > max_bytes:
            raise WorkspaceProcessError("blob_too_large")
        return self.blobs[oid]


async def workspace_import(env, chunker, cache, *, version=None):
    from devflow.knowledge.schemas import WorkspaceImportRequest
    from devflow.knowledge.sources import SourceReader
    from devflow.workspace.read import WorkspaceReader

    version = version or await env.workspace()
    reader = SourceReader(
        env.factory, env.policy, WorkspaceReader(env.factory, env.policy, cache)
    )
    service = KnowledgeService(env.factory, env.policy, chunker=chunker, reader=reader)
    receipt = await service.request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace", repository_id=env.repository.id, workspace_id=version.id
        ),
        uuid4().hex,
    )
    return service, receipt, version


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["files", "bytes", "tree"])
async def test_partial_scan_is_not_complete(
    knowledge_env, chunker, workspace_tmp_path, boundary
):
    env = knowledge_env
    cache = ImportCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"A")
    service, receipt, _ = await workspace_import(env, chunker, cache)
    async with env.factory.begin() as db:
        imported = await db.get(KnowledgeImport, receipt.import_id)
        imported.uploads_done = True
        if boundary == "files":
            # A durable restart at the 2001st eligible candidate.
            imported.eligible_files = 2000
        elif boundary == "bytes":
            imported.scanned_bytes = 33554432
        else:
            cache.truncated = True
    if boundary == "tree":
        # A verified entry in the bounded prefix may be included, while the tail
        # remains explicitly unknown. No claim of native Git execution here.
        await finish(env, service, receipt)
    else:
        await finish(env, service, receipt)
        assert not any(call[0] == "blob" for call in cache.calls)
    async with env.factory() as db:
        imported = await db.get(KnowledgeImport, receipt.import_id)
        assert (
            imported.status == "ready_partial"
            and imported.unscanned
            and not imported.complete
        )
        assert imported.eligible_files <= 2000 and imported.scanned_bytes <= 33554432


@pytest.mark.asyncio
async def test_workspace_entry_without_size_is_skipped_not_budgeted(
    knowledge_env, chunker, workspace_tmp_path
):
    class UnknownSizeCache(ImportCache):
        def blob_size(self, repo, oid):
            return None

    env = knowledge_env
    cache = UnknownSizeCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"no declared size")
    service, receipt, _ = await workspace_import(env, chunker, cache)
    await finish(env, service, receipt)
    async with env.factory() as db:
        imported = await db.get(KnowledgeImport, receipt.import_id)
        member = await db.scalar(
            select(ImportMember).where(ImportMember.import_id == receipt.import_id)
        )
    # An unknown size cannot be charged to the byte budget, so the candidate is
    # skipped with a stable reason and never scanned.
    assert imported.scanned_bytes == 0 and imported.eligible_files == 0
    assert member.status == "skipped" and member.reason == "unknown_size"
    assert member.declared_bytes is None
    assert not any(call[0] == "blob" for call in cache.calls)


@pytest.mark.asyncio
async def test_workspace_tombstone_and_sha_change_preserve_uploads(
    knowledge_env, chunker, workspace_tmp_path
):
    env = knowledge_env
    upload_receipt, _ = await upload(env, b"upload")
    async with env.factory.begin() as db:
        (await db.get(Job, upload_receipt.job_id)).status = "failed"
    cache = ImportCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"workspace")
    service, receipt, _ = await workspace_import(env, chunker, cache)
    await finish(env, service, receipt)
    assert {c.text async for c in service.completed_chunks(receipt.import_id)} == {
        "upload",
        "workspace",
    }
    async with env.factory() as db:
        document = await db.scalar(select(Document).where(Document.kind == "workspace"))
        doc_id = document.id
    deletion = await service.request_import(
        env.actor,
        {"kind": "delete", "repository_id": env.repository.id, "document_id": doc_id},
        "delete-workspace",
    )
    cache.calls.clear()
    await finish(env, service, deletion)
    assert [c.text async for c in service.completed_chunks(deletion.import_id)] == [
        "upload"
    ]
    assert not any(call[0] == "blob" for call in cache.calls)
    newer = await env.workspace()
    async with env.factory.begin() as db:
        from devflow.workspace.models import WorkspaceVersion

        (await db.get(WorkspaceVersion, newer.id)).commit_sha = "d" * 40
    cache.calls.clear()
    service, receipt, _ = await workspace_import(env, chunker, cache, version=newer)
    await finish(env, service, receipt)
    assert [c.text async for c in service.completed_chunks(receipt.import_id)] == [
        "upload"
    ]
    assert not any(call[0] == "blob" for call in cache.calls)


@pytest.mark.asyncio
async def test_completed_chunks_rejects_missing_manifest_member(knowledge_env, chunker):
    from devflow.errors import AppError

    env = knowledge_env
    receipt, _ = await upload(env)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)
    async with env.factory.begin() as db:
        revision = await db.scalar(select(DocumentRevision))
        revision.searchable, revision.availability = False, "unavailable"
    with pytest.raises(AppError):
        _ = [c async for c in service.completed_chunks(receipt.import_id)]


@pytest.mark.asyncio
async def test_expired_recovery_counts_once_and_deadline_is_original(
    knowledge_env, chunker
):
    env = knowledge_env
    receipt, _ = await upload(env)
    lease = await claim(env, receipt.job_id)
    async with env.factory.begin() as db:
        (await db.get(Job, lease.job_id)).lease_expires_at = await db.scalar(
            select(func.clock_timestamp())
        ) - timedelta(days=1)
    queue = Queue(env.factory)
    assert await queue.recover_expired() == 1
    assert await queue.recover_expired() == 0
    assert not await queue.fail(lease, "attempt_timeout", True)
    async with env.factory.begin() as db:
        row = await db.get(KnowledgeImport, receipt.import_id)
        assert row.failures == 1
        row.created_at = await db.scalar(select(func.clock_timestamp())) - timedelta(
            days=2
        )
        row.deadline = row.created_at + timedelta(hours=24)
    lease = await claim(env, receipt.job_id)
    from devflow.errors import AppError

    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    with pytest.raises(AppError):
        await service.import_batch(lease)
    assert await queue.fail(lease, "budget_exhausted", False)
    async with env.factory() as db:
        assert (await db.get(KnowledgeImport, receipt.import_id)).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", ["repository", "global", "bytes"])
async def test_concurrent_capacity_admission(
    knowledge_env, chunker, workspace_tmp_path, capacity
):
    import asyncio

    from support.repository_env import clear_initial_sync, repository

    from devflow.errors import AppError
    from devflow.knowledge.models import KnowledgeCapacity, RepositoryCapacity

    env = knowledge_env
    original = env.repository
    env.repositories[(11, 1)] = ([repository(), repository(303, "team/other")], None)
    other = await env.service.register(env.actor, 11, 303)
    await clear_initial_sync(env)
    if capacity == "bytes":
        service = KnowledgeService(env.factory, env.policy)
        async with env.factory.begin() as db:
            db.add(KnowledgeCapacity(id=1, body_bytes=134217727))
        results = await asyncio.gather(
            service.stage_upload(env.actor, original.id, "a.md", b"A"),
            service.stage_upload(env.actor, other.id, "b.md", b"B"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, AppError) for item in results) == 1
        async with env.factory() as db:
            assert (await db.get(KnowledgeCapacity, 1)).body_bytes == 134217728
        return
    a, _ = await upload(env, b"A")
    env.repository = other
    b, _ = await upload(env, b"B")
    env.repository = original
    async with env.factory.begin() as db:
        if capacity == "global":
            (await db.get(KnowledgeCapacity, 1)).chunks = 39999
        else:
            (await db.get(RepositoryCapacity, original.id)).chunks = 10000
    la, lb = await claim(env, a.job_id), await claim(env, b.job_id)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    results = await asyncio.gather(
        service.import_batch(la), service.import_batch(lb), return_exceptions=True
    )
    assert (
        sum(
            isinstance(item, AppError) and item.code == "storage_exhausted"
            for item in results
        )
        == 1
    )
    async with env.factory() as db:
        assert (await db.get(KnowledgeCapacity, 1)).chunks <= 40000
        assert (await db.get(RepositoryCapacity, original.id)).chunks <= 10000


@pytest.mark.asyncio
async def test_chunk_event_successor_rollback_and_retry(
    knowledge_env, chunker, monkeypatch
):
    from devflow.knowledge.models import Chunk, KnowledgeCapacity

    env = knowledge_env
    receipt, _ = await upload(env)
    lease = await claim(env, receipt.job_id)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    original = Queue._transition

    async def broken(db, job, action):
        if action == "job.submitted":
            raise RuntimeError("injected before successor commit")
        await original(db, job, action)

    monkeypatch.setattr(Queue, "_transition", staticmethod(broken))
    with pytest.raises(RuntimeError):
        await service.import_batch(lease)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 0
        assert (await db.get(KnowledgeCapacity, 1)).chunks == 0
        assert (await db.get(Job, lease.job_id)).status == "running"
        assert (await db.get(KnowledgeImport, receipt.import_id)).included == 0
    monkeypatch.setattr(Queue, "_transition", staticmethod(original))
    await service.import_batch(lease)
    await service.import_batch(lease)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "knowledge.import", Job.status == "queued")
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["disabled", "role", "auth_version", "github_id", "expiry", "status", "binding"],
)
async def test_actor_source_and_lease_fences(knowledge_env, chunker, mutation):
    from devflow.auth.models import User
    from devflow.errors import AppError
    from devflow.knowledge.models import Chunk
    from devflow.repositories.models import Repository

    env = knowledge_env
    receipt, _ = await upload(env)
    lease = await claim(env, receipt.job_id)
    async with env.factory.begin() as db:
        user = await db.get(User, env.actor.id)
        if mutation == "disabled":
            user.enabled = False
        elif mutation == "role":
            user.role = "member"
        elif mutation == "auth_version":
            user.auth_version += 1
        elif mutation == "github_id":
            user.github_id += 1
        elif mutation == "expiry":
            (await db.get(Job, lease.job_id)).lease_expires_at = await db.scalar(
                select(func.clock_timestamp())
            ) - timedelta(days=1)
        elif mutation == "status":
            (await db.get(Job, lease.job_id)).status = "queued"
        else:
            (await db.get(Repository, env.repository.id)).auth_version += 1
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    try:
        await service.import_batch(lease)
    except AppError:
        pass
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 0
        assert (await db.get(KnowledgeImport, receipt.import_id)).included == 0


@pytest.mark.asyncio
async def test_replacement_and_stage_cleanup_preserve_only_accepted_target(
    knowledge_env, chunker
):
    from sqlalchemy import delete

    from devflow.knowledge.models import UploadStage

    env = knowledge_env
    a1, document = await upload(env, b"old")
    a2, _ = await upload(env, b"new", document)
    b, _ = await upload(env, b"B")
    async with env.factory.begin() as db:
        for receipt in (a1, a2):
            (await db.get(Job, receipt.job_id)).status = "failed"
        await db.execute(
            delete(UploadStage).where(
                UploadStage.consumed_import_id.in_((a1.import_id, a2.import_id))
            )
        )
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, b)
    assert {c.text async for c in service.completed_chunks(b.import_id)} == {"new", "B"}


@pytest.mark.asyncio
async def test_completed_empty_member_still_requires_availability(
    knowledge_env, chunker
):
    from devflow.errors import AppError

    env = knowledge_env
    receipt, _ = await upload(env, b"")
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, receipt)
    async with env.factory.begin() as db:
        revision = await db.scalar(select(DocumentRevision))
        revision.searchable, revision.availability = False, "unavailable"
    with pytest.raises(AppError):
        _ = [c async for c in service.completed_chunks(receipt.import_id)]


@pytest.mark.asyncio
async def test_truncated_raw_requires_positive_matching_object(
    knowledge_env, workspace_tmp_path
):
    from devflow.errors import AppError
    from devflow.workspace.read import WorkspaceReader

    env = knowledge_env
    version = await env.workspace()
    cache = ImportCache(workspace_tmp_path)
    oid = cache.add(b"docs/a.md", b"exact")
    cache.truncated = True
    reader = WorkspaceReader(env.factory, env.policy, cache)
    for path, expected in (
        ("docs/a.md", None),
        ("docs/a.md", "0" * 40),
        ("docs/missing.md", oid),
    ):
        with pytest.raises(AppError):
            await reader.raw(
                env.actor,
                env.repository.id,
                version.id,
                path,
                sha=version.commit_sha,
                expected_oid=expected,
            )
    assert not any(call[0] == "blob" for call in cache.calls)
    assert (
        await reader.raw(
            env.actor,
            env.repository.id,
            version.id,
            "docs/a.md",
            sha=version.commit_sha,
            expected_oid=oid,
        )
        == b"exact"
    )


@pytest.mark.asyncio
async def test_tokenizer_cancellation_drains_owned_work(
    knowledge_env, chunker, monkeypatch
):
    import asyncio
    import threading

    env = knowledge_env
    receipt, _ = await upload(env)
    lease = await claim(env, receipt.job_id)
    entered, release = threading.Event(), threading.Event()
    original = chunker.split

    def slow(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args)

    monkeypatch.setattr(chunker, "split", slow)
    task = asyncio.create_task(
        KnowledgeService(env.factory, env.policy, chunker=chunker).import_batch(lease)
    )
    while not entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    drained = not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained, "cancellation must retain ownership until tokenizer work stops"
    async with env.factory() as db:
        assert (await db.get(KnowledgeImport, receipt.import_id)).included == 0


@pytest.mark.asyncio
async def test_pending_upload_inheritance_uses_new_admin_and_rejects_unaccepted_stage(
    knowledge_env, chunker
):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.errors import AppError
    from devflow.github.models import GitHubCredential
    from devflow.knowledge.models import UploadStage
    from devflow.knowledge.sources import SourceReader

    env = knowledge_env
    first, document = await upload(env, b"accepted A")
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    unaccepted = await service.stage_upload(
        env.actor, env.repository.id, "secret.md", b"not accepted"
    )
    async with env.factory.begin() as db:
        user = User(github_id=43, role="admin")
        db.add(user)
        await db.flush()
        db.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=env.cipher.encrypt(SecretStr("user-token")),
            )
        )
        actor = Actor(user.id, user.github_id, user.role, user.auth_version)
        (await db.get(Job, first.job_id)).status = "failed"
    env.actor = actor
    second, _ = await upload(env, b"B")
    reader = SourceReader(env.factory, env.policy)
    with pytest.raises(AppError):
        await reader.read_import(actor, first.import_id)
    await finish(env, service, second)
    assert {c.text async for c in service.completed_chunks(second.import_id)} == {
        "accepted A",
        "B",
    }
    async with env.factory() as db:
        assert (await db.get(UploadStage, unaccepted)).consumed_import_id is None
        assert (await db.get(Document, document)).accepted_revision_id is not None


@pytest.mark.asyncio
async def test_fresh_proof_target_drift_conflicts_without_consuming_stage(
    knowledge_env, monkeypatch
):
    from devflow.errors import AppError
    from devflow.knowledge.models import KnowledgeCorpus, UploadStage

    env = knowledge_env
    await upload(env, b"A")
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "b.md", b"B")
    original = service.access.verify

    async def drift(*args, **kwargs):
        grant = await original(*args, **kwargs)
        async with env.factory.begin() as db:
            (await db.get(KnowledgeCorpus, env.repository.id)).target_generation += 1
        return grant

    monkeypatch.setattr(service.access, "verify", drift)
    with pytest.raises(AppError) as error:
        await service.request_import(
            env.actor,
            UploadImportRequest(
                kind="upload",
                repository_id=env.repository.id,
                staging_id=stage,
                filename="b.md",
            ),
            "drift",
        )
    assert error.value.code == "idempotency_conflict"
    async with env.factory() as db:
        assert (await db.get(UploadStage, stage)).consumed_import_id is None


@pytest.mark.asyncio
async def test_workspace_switch_and_other_actual_source_do_not_resurrect_tombstone(
    knowledge_env, chunker, workspace_tmp_path
):
    from devflow.knowledge.models import ImportRepositoryEpoch

    env = knowledge_env
    cache = ImportCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"original source")
    service, receipt, _ = await workspace_import(env, chunker, cache)
    await finish(env, service, receipt)
    async with env.factory() as db:
        document = await db.scalar(select(Document).where(Document.kind == "workspace"))
    deletion = await service.request_import(
        env.actor,
        {
            "kind": "delete",
            "repository_id": env.repository.id,
            "document_id": document.id,
        },
        "delete-original",
    )
    await finish(env, service, deletion)
    fork = await env.fork()
    fork_version = await env.workspace(fork)
    cache.entries.clear()
    cache.add(b"docs/a.md", b"fork source")
    service, new, _ = await workspace_import(env, chunker, cache, version=fork_version)
    await finish(env, service, new)
    assert [c.text async for c in service.completed_chunks(new.import_id)] == [
        "fork source"
    ]
    base_version = await env.workspace()
    env.repositories[(12, 1)] = ([], None)
    service, base, _ = await workspace_import(env, chunker, cache, version=base_version)
    await finish(env, service, base)
    assert [c async for c in service.completed_chunks(base.import_id)] == []
    async with env.factory() as db:
        assert set(
            await db.scalars(
                select(ImportRepositoryEpoch.repository_id).where(
                    ImportRepositoryEpoch.import_id == base.import_id
                )
            )
        ) == {env.repository.id}


@pytest.mark.asyncio
async def test_completed_upload_carry_forward_pins_new_policy(knowledge_env, chunker):
    from devflow.knowledge.models import Chunk, KnowledgeCapacity

    env = knowledge_env
    a, _ = await upload(env, b"A")
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await finish(env, service, a)
    b, _ = await upload(env, b"B")
    chunker.tokenizer_fingerprint = "f" * 64
    await finish(env, service, b)
    manifest = [c async for c in service.completed_chunks(b.import_id)]
    assert {c.text for c in manifest} == {"A", "B"}
    assert {c.tokenizer_fingerprint for c in manifest} == {"f" * 64}
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Chunk)) == 3
        assert (await db.get(KnowledgeCapacity, 1)).body_bytes == 2


@pytest.mark.asyncio
async def test_upload_after_partial_workspace_does_not_claim_complete(
    knowledge_env, chunker, workspace_tmp_path
):
    env = knowledge_env
    cache = ImportCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"workspace")
    cache.truncated = True
    service, first, _ = await workspace_import(env, chunker, cache)
    await finish(env, service, first)
    second, _ = await upload(env, b"upload")
    await finish(env, service, second)
    assert {c.text async for c in service.completed_chunks(second.import_id)} == {
        "workspace",
        "upload",
    }
    async with env.factory() as db:
        row = await db.get(KnowledgeImport, second.import_id)
        assert row.status == "ready_partial" and row.unscanned and not row.complete


@pytest.mark.asyncio
async def test_failure_limit_across_successor_and_worker_completion(
    knowledge_env, chunker
):
    from devflow.knowledge.handlers import KnowledgeImportHandler
    from devflow.worker import _execute

    env = knowledge_env
    receipt, _ = await upload(env, b"large " * 2000)
    queue = Queue(env.factory, retry_first_seconds=0, retry_second_seconds=0)
    lease = await claim(env, receipt.job_id)
    assert await queue.attempt_timeout(lease, 600) == 120
    assert await queue.fail(lease, "attempt_timeout", True)
    lease = await claim(env, receipt.job_id)
    service = KnowledgeService(env.factory, env.policy, chunker=chunker)
    await _execute(queue, lease, KnowledgeImportHandler(service).run)
    async with env.factory() as db:
        assert (await db.get(Job, lease.job_id)).status == "succeeded"
        successor = await db.scalar(
            select(Job.id).where(Job.kind == "knowledge.import", Job.status == "queued")
        )
    assert successor != receipt.job_id
    for number in (2, 3):
        lease = await claim(env, successor)
        assert await queue.fail(lease, "attempt_timeout", True)
        async with env.factory() as db:
            imported = await db.get(KnowledgeImport, receipt.import_id)
            assert imported.failures == number
    assert imported.status == "failed"


@pytest.mark.asyncio
async def test_unique_identity_conflict_never_reuses_other_exact_path(
    knowledge_env, chunker, workspace_tmp_path
):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    env = knowledge_env
    cache = ImportCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"A")
    cache.add(b"docs/b.md", b"B")
    service, receipt, _ = await workspace_import(env, chunker, cache)
    # Force a uniqueness collision in this isolated DB, exercising the same
    # failed insert path as a bounded digest collision without claiming MD5 proof.
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE UNIQUE INDEX test_knowledge_collision ON knowledge_documents(repository_id,source_repository_id,(1)) WHERE kind='workspace'"
            )
        )
    with pytest.raises(IntegrityError):
        await finish(env, service, receipt)
    async with env.factory() as db:
        assert list(
            await db.scalars(select(Document.path).where(Document.kind == "workspace"))
        ) == ["docs/a.md"]
        member = await db.scalar(
            select(ImportMember).where(
                ImportMember.import_id == receipt.import_id,
                ImportMember.source_key == "docs/b.md",
            )
        )
        assert member.revision_id is None and member.status == "pending"
