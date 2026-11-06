"""Finite import steps; source/tokenizer work is outside short fenced commits."""

import asyncio
import hashlib
import threading
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import KnowledgeImportInput
from devflow.knowledge.chunking import CHUNKING_VERSION
from devflow.knowledge.indexing import enqueue_index_for_import
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    KnowledgeCorpus,
    KnowledgeImport,
    RepositoryCapacity,
    RevisionDependency,
)
from devflow.knowledge.schemas import ImportProgress, SourceBytes, SourceIdentity
from devflow.knowledge.sources import (
    PATH_RULE_VERSION,
    SourceReader,
    path_reason,
    workspace_source_version,
)
from devflow.sync.scope import transaction
from devflow.workspace.handlers import owned_thread
from devflow.workspace.models import WorkspaceVersion
from devflow.workspace.read import RawSourceError


def changed():
    return AppError("version_changed", 409, "Knowledge target changed")


async def context(service, import_id):
    async with service.factory() as db:
        row = await db.get(KnowledgeImport, import_id)
        if row is None:
            raise not_found()
        actor = Actor(
            row.actor_id, row.actor_github_id, "admin", row.actor_auth_version
        )
        ids = tuple(
            await db.scalars(
                select(ImportRepositoryEpoch.repository_id).where(
                    ImportRepositoryEpoch.import_id == row.id
                )
            )
        )
    grant = await service.access.verify(actor, ids, fresh=True)
    return row, grant


@asynccontextmanager
async def fenced(service, lease, imported, grant):
    async with transaction(service.factory) as db:
        capacity = await service._capacity(db, imported.repository_id)
        await service.access.lock(db, grant)
        await service._admin(db, grant.actor)
        await service._initialize_repository_capacity(db, imported.repository_id)
        job, now = await Queue(service.factory)._owned(db, lease)
        if job is None:
            yield None
            return
        if (
            job.kind != "knowledge.import"
            or job.repository_id != imported.repository_id
            or job.requested_by != imported.actor_id
        ):
            raise changed()
        payload = KnowledgeImportInput.model_validate(job.input)
        corpus = await db.get(
            KnowledgeCorpus, imported.repository_id, with_for_update=True
        )
        row = await db.get(
            KnowledgeImport, imported.id, with_for_update=True, populate_existing=True
        )
        await service._original_epochs(db, row, grant)
        if (
            payload.import_id != row.id
            or payload.target_generation != row.target_generation
            or row.target_generation != corpus.target_generation
        ):
            yield None
            return
        if row.status not in {"queued", "building"}:
            yield None
            return
        if row.composition_version != 1 or row.path_rule_version != PATH_RULE_VERSION:
            raise changed()
        if now >= row.deadline or row.failures >= 3:
            raise AppError("budget_exhausted", 409, "Import budget exhausted")
        repository_capacity = await db.get(RepositoryCapacity, imported.repository_id)
        yield db, row, job, now, capacity, repository_capacity
        final_now = await db.scalar(select(func.clock_timestamp()))
        # Job may have been completed inside this transaction; expiry was captured
        # before clearing the lease. Check the captured deadline at the very end.
        if final_now >= row.deadline or final_now >= job._import_expiry:
            raise AppError("lease_expired", 409, "Import lease expired")


def progress(row):
    return ImportProgress(
        import_id=row.id,
        status=row.status,
        processed=row.processed_chunks,
        complete=row.status in {"ready", "ready_partial"},
    )


async def finish_step(db, row, job, now, *, final=False):
    job._import_expiry = job.lease_expires_at
    row.status = (
        (
            "ready_partial"
            if row.unscanned or row.skipped or row.unsupported or row.over_limit
            else "ready"
        )
        if final
        else "building"
    )
    row.complete = final and not row.unscanned
    job.status, job.error_code = "succeeded", None
    job.result = {
        "import_id": str(row.id),
        "target_generation": row.target_generation,
        "status": row.status,
    }
    job.lease_owner = job.lease_expires_at = None
    job.updated_at = now
    await Queue._transition(db, job, "job.succeeded")
    if not final:
        successor = Job(
            kind="knowledge.import",
            repository_id=row.repository_id,
            requested_by=row.actor_id,
            input=KnowledgeImportInput(
                import_id=row.id, target_generation=row.target_generation
            ).model_dump(mode="json"),
            input_hash=row.request_hash,
            idempotency_key=uuid4().hex,
            max_attempts=3,
        )
        db.add(successor)
        await db.flush()
        await Queue._transition(db, successor, "job.submitted")


async def discover_uploads(service, lease, imported, grant):
    async with fenced(service, lease, imported, grant) as state:
        if state is None:
            return progress(imported)
        db, row, job, now, _, _ = state
        job._import_expiry = job.lease_expires_at
        query = select(Document).where(
            Document.repository_id == row.repository_id,
            Document.kind == "upload",
            Document.deleted.is_(False),
            Document.accepted_revision_id.is_not(None),
        )
        if row.uploads_after:
            query = query.where(Document.id > row.uploads_after)
        documents = list(await db.scalars(query.order_by(Document.id).limit(100)))
        for document in documents:
            existing = await db.scalar(
                select(ImportMember).where(
                    ImportMember.import_id == row.id,
                    ImportMember.member_kind == "upload",
                    ImportMember.source_key == str(document.id),
                )
            )
            if existing:
                if existing.revision_id != document.accepted_revision_id:
                    raise changed()
            else:
                db.add(
                    ImportMember(
                        import_id=row.id,
                        member_kind="upload",
                        source_key=str(document.id),
                        ordinal=row.cursor,
                        revision_id=document.accepted_revision_id,
                    )
                )
                row.cursor += 1
            row.uploads_after = document.id
        row.uploads_done = len(documents) < 100
        await finish_step(db, row, job, now)
        return progress(row)


async def discover_workspace(service, lease, imported, grant):
    reader = service.reader
    if reader is None or reader.workspace is None:
        raise AppError("service_unavailable", 503, "Workspace reader unavailable")
    page = await reader.workspace.metadata(
        grant.actor,
        imported.repository_id,
        imported.target_workspace_id,
        sha=imported.target_sha,
        cursor=imported.workspace_cursor,
        limit=16,
    )
    async with fenced(service, lease, imported, grant) as state:
        if state is None:
            return progress(imported)
        db, row, job, now, _, _ = state
        job._import_expiry = job.lease_expires_at
        version = await db.get(WorkspaceVersion, row.target_workspace_id)
        for entry in page.items:
            if entry.path is None:
                row.skipped += 1
                row.workspace_cursor += 1
                continue
            reason = path_reason(entry.path, **row.target_path_scopes)
            status = (
                "unsupported"
                if reason == "unsupported"
                else "skipped"
                if reason
                else "pending"
            )
            if status == "pending" and entry.mode not in {"100644", "100755"}:
                status, reason = (
                    "skipped",
                    "symlink" if entry.mode == "120000" else "gitlink",
                )
            document = await db.scalar(
                select(Document).where(
                    Document.repository_id == row.repository_id,
                    Document.source_repository_id == version.source_repository_id,
                    Document.kind == "workspace",
                    Document.path == entry.path,
                )
            )
            if document and document.deleted:
                status, reason = "skipped", "source_deleted"
            if status == "pending" and entry.size is None:
                # Without a declared size the byte budget cannot be enforced.
                status, reason = "skipped", "unknown_size"
            if status == "pending":
                if (
                    row.eligible_files >= 2000
                    or row.scanned_bytes + entry.size > 33554432
                ):
                    status, reason = "over_limit", "scan_limit"
                    row.workspace_done, row.unscanned, row.scan_reason = (
                        True,
                        True,
                        reason,
                    )
                else:
                    row.eligible_files += 1
                    if entry.size > 1048576:
                        status, reason = "over_limit", "too_large"
                    else:
                        row.scanned_bytes += entry.size
            db.add(
                ImportMember(
                    import_id=row.id,
                    member_kind="workspace",
                    source_key=entry.path,
                    ordinal=row.cursor,
                    status=status,
                    reason=reason,
                    blob_oid=entry.oid,
                    declared_bytes=entry.size
                    if entry.size is not None and entry.size <= 2147483647
                    else None,
                )
            )
            row.cursor += 1
            if status != "pending":
                setattr(row, status, getattr(row, status) + 1)
            row.workspace_cursor += 1
            if row.workspace_done:
                break
        if not row.workspace_done and page.next_cursor is None:
            row.workspace_done = True
            row.unscanned = not page.exhausted
            row.scan_reason = "tree_limit" if page.truncated else None
        await finish_step(db, row, job, now)
        return progress(row)


async def materialize(service, lease, imported, grant, member):
    reader = service.reader or SourceReader(service.factory, service.policy)
    if member.revision_id:
        return await reader.read_member(grant.actor, imported.id, member.id)
    body = await reader.read_target(grant.actor, imported.id, member.source_key)
    digest = hashlib.sha256(body).hexdigest()
    async with fenced(service, lease, imported, grant) as state:
        if state is None:
            return None
        db, row, job, _, capacity, _ = state
        job._import_expiry = job.lease_expires_at
        current = await db.get(ImportMember, member.id, with_for_update=True)
        version = await db.get(WorkspaceVersion, row.target_workspace_id)
        dependencies = tuple(
            sorted({UUID(item["repository_id"]) for item in version.scopes}, key=str)
        )
        document = await db.scalar(
            select(Document)
            .where(
                Document.repository_id == row.repository_id,
                Document.source_repository_id == version.source_repository_id,
                Document.kind == "workspace",
                Document.path == member.source_key,
            )
            .with_for_update()
        )
        if document and document.deleted:
            raise changed()
        if document is None:
            document = Document(
                repository_id=row.repository_id,
                source_repository_id=version.source_repository_id,
                kind="workspace",
                path=member.source_key,
            )
            db.add(document)
            await db.flush()
        identity = SourceIdentity(
            repository_id=row.repository_id,
            source_repository_id=version.source_repository_id,
            dependency_repository_ids=dependencies,
            document_id=document.id,
            revision_hash=digest,
            workspace_id=version.id,
            sha=version.commit_sha,
            path=document.path,
        )
        key = workspace_source_version(
            **identity.model_dump(exclude={"document_id", "revision_hash"})
        )
        revision = await db.scalar(
            select(DocumentRevision)
            .where(
                DocumentRevision.document_id == document.id,
                DocumentRevision.source_version == key,
                DocumentRevision.revision_hash == digest,
            )
            .with_for_update()
        )
        if revision is None:
            if capacity.body_bytes + capacity.reserved_bytes + len(body) > 134217728:
                raise AppError("storage_exhausted", 409, "Knowledge capacity exhausted")
            revision = DocumentRevision(
                document_id=document.id,
                revision_hash=digest,
                source_version=key,
                workspace_id=version.id,
                sha=version.commit_sha,
                body=body,
                body_bytes=len(body),
            )
            db.add(revision)
            await db.flush()
            for repo in dependencies:
                db.add(RevisionDependency(revision_id=revision.id, repository_id=repo))
            capacity.body_bytes += len(body)
        else:
            actual = set(
                await db.scalars(
                    select(RevisionDependency.repository_id).where(
                        RevisionDependency.revision_id == revision.id
                    )
                )
            )
            if (
                actual != set(dependencies)
                or (revision.workspace_id, revision.sha)
                != (version.id, version.commit_sha)
                or revision.body_bytes != len(body)
            ):
                raise changed()
            if revision.availability == "deleted":
                # Tombstones are never resurrected by a re-import.
                raise changed()
            if revision.body is None:
                if revision.availability != "expired":
                    raise changed()
                # The same immutable identity was reclaimed earlier. Restore the
                # canonical bytes under the held capacity lock and reuse the row;
                # the unique (document, source_version, hash) key must stay usable.
                if (
                    capacity.body_bytes + capacity.reserved_bytes + len(body)
                    > 134217728
                ):
                    raise AppError(
                        "storage_exhausted", 409, "Knowledge capacity exhausted"
                    )
                revision.body = body
                revision.availability = "available"
                revision.searchable = False
                capacity.body_bytes += len(body)
            elif revision.availability != "available" or revision.body != body:
                raise changed()
        if current.declared_bytes != len(body):
            raise changed()
        current.revision_id = revision.id
        return SourceBytes(identity=identity, body=body)


async def import_batch(service, lease):
    started = time.monotonic()
    async with asyncio.timeout(120):
        async with service.factory() as db:
            job = await db.scalar(
                select(Job).where(
                    Job.id == lease.job_id,
                    Job.status == "running",
                    Job.lease_owner == lease.owner,
                    Job.lease_generation == lease.generation,
                    Job.attempt == lease.attempt,
                    Job.lease_expires_at > func.clock_timestamp(),
                )
            )
            if job is None:
                # A stale lease is a strict no-op, including no authorization side effects.
                return None
            payload = KnowledgeImportInput.model_validate(job.input)
            imported = await db.get(KnowledgeImport, payload.import_id)
            corpus = await db.get(KnowledgeCorpus, imported.repository_id)
            if imported.target_generation != corpus.target_generation:
                return progress(imported)
        imported, grant = await context(service, imported.id)
        async with service.factory() as db:
            member = await db.scalar(
                select(ImportMember)
                .where(
                    ImportMember.import_id == imported.id,
                    ImportMember.status == "pending",
                )
                .order_by(ImportMember.ordinal)
                .limit(1)
            )
        if member is None:
            if not imported.uploads_done:
                return await discover_uploads(service, lease, imported, grant)
            if not imported.workspace_done:
                return await discover_workspace(service, lease, imported, grant)
            return await publish(service, lease, imported, grant)
        try:
            source = await materialize(service, lease, imported, grant, member)
        except RawSourceError as error:
            async with fenced(service, lease, imported, grant) as state:
                if state is None:
                    return progress(imported)
                db, row, job, now, _, _ = state
                current = await db.get(ImportMember, member.id, with_for_update=True)
                current.status, current.reason = "unsupported", error.reason
                row.unsupported += 1
                await finish_step(db, row, job, now)
                return progress(row)
        if source is None:
            return progress(imported)
        if service.chunker is None:
            raise AppError("service_unavailable", 503, "Local tokenizer unavailable")
        # Keep ownership through cancellation; the pure tokenizer never writes DB.
        cancel = threading.Event()
        try:
            drafts = await owned_thread(
                lambda: service.chunker.split(
                    source, "", cancel=cancel, deadline=started + 115
                ),
                cancel,
            )
        except ValueError:
            raise AppError("source_limit", 422, "Source chunking failed") from None
        if time.monotonic() - started >= 115:
            raise AppError("attempt_timeout", 503, "Import attempt timed out")
        async with fenced(service, lease, imported, grant) as state:
            if state is None:
                return progress(imported)
            db, row, job, now, capacity, repo_capacity = state
            current = await db.get(
                ImportMember, member.id, with_for_update=True, populate_existing=True
            )
            fingerprint = service.chunker.tokenizer_fingerprint
            if row.tokenizer_fingerprint and (
                row.tokenizer_fingerprint,
                row.chunking_version,
            ) != (fingerprint, CHUNKING_VERSION):
                raise changed()
            row.tokenizer_fingerprint, row.chunking_version = (
                fingerprint,
                CHUNKING_VERSION,
            )
            if current.total_chunks is not None and current.total_chunks != len(drafts):
                raise changed()
            current.total_chunks = len(drafts)
            for ordinal in range(
                current.next_chunk, min(current.next_chunk + 16, len(drafts))
            ):
                draft = drafts[ordinal]
                chunk = await db.scalar(
                    select(Chunk).where(
                        Chunk.revision_id == current.revision_id,
                        Chunk.tokenizer_fingerprint == fingerprint,
                        Chunk.chunking_version == CHUNKING_VERSION,
                        Chunk.ordinal == ordinal,
                    )
                )
                if chunk is None:
                    if (
                        capacity.chunks + capacity.reserved_chunks >= 40000
                        or repo_capacity.chunks + repo_capacity.reserved_chunks >= 10000
                    ):
                        raise AppError(
                            "storage_exhausted", 409, "Knowledge capacity exhausted"
                        )
                    db.add(
                        Chunk(
                            revision_id=current.revision_id,
                            ordinal=ordinal,
                            **draft.model_dump(exclude={"location"}),
                            **draft.location.model_dump(),
                        )
                    )
                    capacity.chunks += 1
                    repo_capacity.chunks += 1
                elif (
                    chunk.text,
                    chunk.embedding_text,
                    chunk.content_hash,
                    chunk.start_byte,
                    chunk.end_byte,
                ) != (
                    draft.text,
                    draft.embedding_text,
                    draft.content_hash,
                    draft.location.start_byte,
                    draft.location.end_byte,
                ):
                    raise changed()
                current.next_chunk = ordinal + 1
                row.processed_chunks += 1
            if current.next_chunk == current.total_chunks:
                current.status = "included"
                row.included += 1
            await finish_step(db, row, job, now)
            return progress(row)


async def publish(service, lease, imported, grant):
    async with fenced(service, lease, imported, grant) as state:
        if state is None:
            return progress(imported)
        db, row, job, now, _, _ = state
        if await db.scalar(
            select(ImportMember.id)
            .where(ImportMember.import_id == row.id, ImportMember.status == "pending")
            .limit(1)
        ):
            raise changed()
        if not row.uploads_done or not row.workspace_done:
            raise changed()
        revisions = select(ImportMember.revision_id).where(
            ImportMember.import_id == row.id, ImportMember.status == "included"
        )
        invalid = await db.scalar(
            select(DocumentRevision.id)
            .join(Document, Document.id == DocumentRevision.document_id)
            .where(
                DocumentRevision.id.in_(revisions),
                (
                    Document.deleted.is_(True)
                    | (DocumentRevision.availability != "available")
                    | DocumentRevision.body.is_(None)
                ),
            )
            .limit(1)
        )
        if invalid:
            raise changed()
        await db.execute(
            update(DocumentRevision)
            .where(DocumentRevision.id.in_(revisions))
            .values(
                published_at=func.coalesce(DocumentRevision.published_at, now),
                searchable=True,
            )
        )
        # The composed manifest is the whole active selection. Revisions that
        # belonged to the previous generation must stop advertising themselves
        # as searchable in the same fenced publication.
        await db.execute(
            update(DocumentRevision)
            .where(
                DocumentRevision.searchable.is_(True),
                DocumentRevision.id.not_in(revisions),
                DocumentRevision.document_id.in_(
                    select(Document.id).where(
                        Document.repository_id == row.repository_id
                    )
                ),
            )
            .values(searchable=False)
        )
        if row.target_workspace_id is None:
            row.unscanned = False
        await finish_step(db, row, job, now, final=True)
        # The imported manifest is now immutable and matches the corpus
        # target; request its same-generation index in the same commit so a
        # crash cannot leave a composed target without a build.
        await enqueue_index_for_import(db, row, grant, now)
        return progress(row)


async def completed_chunks(service, import_id):
    imported, grant = await context(service, import_id)
    after = None
    while True:
        async with transaction(service.factory) as db:
            await service.access.lock(db, grant)
            await service._original_epochs(db, imported, grant)
            corpus = await db.get(
                KnowledgeCorpus, imported.repository_id, with_for_update=True
            )
            row = await db.get(KnowledgeImport, import_id)
            if (
                row.composition_version != 1
                or row.status not in {"ready", "ready_partial"}
                or corpus.target_generation != row.target_generation
            ):
                raise changed()
            invalid = await db.scalar(
                select(ImportMember.id)
                .outerjoin(
                    DocumentRevision, DocumentRevision.id == ImportMember.revision_id
                )
                .outerjoin(Document, Document.id == DocumentRevision.document_id)
                .where(
                    ImportMember.import_id == row.id,
                    ImportMember.status == "included",
                    (
                        DocumentRevision.id.is_(None)
                        | Document.deleted.is_(True)
                        | DocumentRevision.body.is_(None)
                        | (DocumentRevision.availability != "available")
                        | DocumentRevision.searchable.is_(False)
                        | DocumentRevision.published_at.is_(None)
                    ),
                )
                .limit(1)
            )
            if invalid:
                raise changed()
            expected, member_count = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(ImportMember.total_chunks), 0),
                        func.count(),
                    ).where(
                        ImportMember.import_id == row.id,
                        ImportMember.status == "included",
                    )
                )
            ).one()
            if member_count != row.included or await db.scalar(
                select(ImportMember.id)
                .where(
                    ImportMember.import_id == row.id,
                    ImportMember.status == "included",
                    (
                        ImportMember.total_chunks.is_(None)
                        | (ImportMember.next_chunk != ImportMember.total_chunks)
                    ),
                )
                .limit(1)
            ):
                raise changed()
            query = (
                select(Chunk)
                .join(ImportMember, ImportMember.revision_id == Chunk.revision_id)
                .join(DocumentRevision, Chunk.revision_id == DocumentRevision.id)
                .join(Document, Document.id == DocumentRevision.document_id)
                .where(
                    ImportMember.import_id == row.id,
                    ImportMember.status == "included",
                    ImportMember.next_chunk == ImportMember.total_chunks,
                    Chunk.ordinal < ImportMember.total_chunks,
                    Chunk.tokenizer_fingerprint == row.tokenizer_fingerprint,
                    Chunk.chunking_version == row.chunking_version,
                    Document.deleted.is_(False),
                    DocumentRevision.searchable.is_(True),
                    DocumentRevision.availability == "available",
                    DocumentRevision.published_at.is_not(None),
                )
            )
            actual = await db.scalar(select(func.count()).select_from(query.subquery()))
            if actual != expected:
                raise changed()
            if after:
                query = query.where(Chunk.id > after)
            batch = list(await db.scalars(query.order_by(Chunk.id).limit(100)))
        if not batch:
            return
        for chunk in batch:
            yield chunk
        after = batch[-1].id
