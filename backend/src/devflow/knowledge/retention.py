"""Bounded body reclamation and the periodic cleanup Job producer.

Physical bytes are reclaimed only after every logical owner (accepted upload,
active import, live stage, current corpus, retention, pin) is checked under the
same locks and one PostgreSQL clock. The revision row itself is never deleted:
identity, provenance and capacity metadata survive as ``expired``.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer

from devflow.audit.service import AuditEntry, write_audit
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import KnowledgeCleanupInput, KnowledgeCleanupResult
from devflow.jobs.service import append_event
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    KnowledgeCapacity,
    KnowledgeCorpus,
    KnowledgeImport,
    RepositoryCapacity,
    RevisionPin,
    RevisionRetention,
    UploadStage,
)
from devflow.knowledge.sources import path_reason
from devflow.sync.scope import transaction

CLEANUP_COOLDOWN = timedelta(hours=1)
CLEANUP_INTERVAL = timedelta(seconds=60)
CLEANUP_BATCH = 100
DISCOVERY_BATCH = 100


@dataclass
class RetentionResult:
    examined: int = 0
    reclaimed: int = 0
    deferred: int = 0
    bytes_reclaimed: int = 0
    chunks_reclaimed: int = 0
    wrapped: bool = False
    cursor: UUID | None = None
    protected: set[UUID] = field(default_factory=set)


def _candidate_ids(repository_id: UUID, after: UUID | None):
    query = (
        select(DocumentRevision.id)
        .join(Document, Document.id == DocumentRevision.document_id)
        .where(
            Document.repository_id == repository_id,
            or_(
                DocumentRevision.body.is_not(None),
                exists(
                    select(Chunk.id).where(Chunk.revision_id == DocumentRevision.id)
                ),
            ),
        )
    )
    if after is not None:
        query = query.where(DocumentRevision.id > after)
    return query.order_by(DocumentRevision.id)


def _protected_exists(repository_id, corpus, now):
    """Cheap SQL owners used by the producer; sweep rechecks them under locks."""
    clauses = [
        exists(
            select(ImportMember.id)
            .join(KnowledgeImport, KnowledgeImport.id == ImportMember.import_id)
            .where(
                ImportMember.revision_id == DocumentRevision.id,
                KnowledgeImport.status.in_(("queued", "building")),
                KnowledgeImport.deadline > now,
            )
        ),
        exists(
            select(UploadStage.id).where(
                UploadStage.revision_id == DocumentRevision.id,
                UploadStage.consumed_import_id.is_(None),
                UploadStage.expires_at > now,
            )
        ),
        exists(
            select(RevisionRetention.owner_id).where(
                RevisionRetention.revision_id == DocumentRevision.id,
                RevisionRetention.expires_at > now,
            )
        ),
        exists(
            select(RevisionPin.id).where(
                RevisionPin.revision_id == DocumentRevision.id,
                RevisionPin.expires_at > now,
            )
        ),
        and_(
            Document.kind == "upload",
            Document.deleted.is_(False),
            Document.accepted_revision_id.is_not(None),
            Document.accepted_revision_id == DocumentRevision.id,
        ),
    ]
    if corpus is not None:
        clauses.append(
            exists(
                select(ImportMember.id)
                .join(KnowledgeImport, KnowledgeImport.id == ImportMember.import_id)
                .where(
                    ImportMember.revision_id == DocumentRevision.id,
                    ImportMember.status == "included",
                    KnowledgeImport.repository_id == repository_id,
                    KnowledgeImport.status.in_(("ready", "ready_partial")),
                    KnowledgeImport.composition_version == 1,
                    KnowledgeImport.target_generation == corpus.target_generation,
                )
            )
        )
    return or_(*clauses)


def _workspace_protected(row, corpus):
    """Exact pre-rediscovery owner, mirroring the sweep's path check."""
    return (
        corpus is not None
        and corpus.target_workspace_id is not None
        and corpus.target_sha is not None
        and row.kind == "workspace"
        and not row.deleted
        and row.workspace_id == corpus.target_workspace_id
        and row.sha == corpus.target_sha
        and row.path is not None
        and path_reason(row.path, **dict(corpus.path_scopes or {})) is None
    )


async def _candidate_scan(db, repository_id, corpus, after, now):
    """One bounded exact page; returns (found, next_scan_after).

    Path selection cannot be expressed safely as a coarse SQL predicate: doing
    so would protect every revision of the current Workspace SHA and suppress
    cleanup of paths dropped by a narrower re-import. Evaluate the exact rules
    over at most one cleanup batch and rotate the producer cursor.
    """
    query = (
        select(
            DocumentRevision.id,
            Document.path,
            Document.kind,
            Document.deleted,
            DocumentRevision.workspace_id,
            DocumentRevision.sha,
        )
        .join(Document, Document.id == DocumentRevision.document_id)
        .where(
            Document.repository_id == repository_id,
            or_(
                DocumentRevision.body.is_not(None),
                exists(
                    select(Chunk.id).where(Chunk.revision_id == DocumentRevision.id)
                ),
            ),
            ~_protected_exists(repository_id, corpus, now),
        )
    )
    if after is not None:
        query = query.where(DocumentRevision.id > after)
    rows = (
        await db.execute(query.order_by(DocumentRevision.id).limit(CLEANUP_BATCH))
    ).all()
    for row in rows:
        if not _workspace_protected(row, corpus):
            return True, after
    if len(rows) < CLEANUP_BATCH:
        return False, None
    return False, rows[-1].id


async def retain_sources(
    session,
    owner_kind,
    owner_id,
    revision_ids,
    expires_at,
):
    """Pin canonical bytes to a future query/message until ``expires_at``.

    Rows are locked before insertion so a concurrent sweep cannot clear a body
    after the owner was accepted. A revision whose body is already gone is not
    resurrected; retention is never read authority.
    """
    if owner_kind not in {"query", "message"}:
        raise AppError("validation_error", 422, "Invalid retention owner")
    if not isinstance(revision_ids, tuple) or len(revision_ids) > CLEANUP_BATCH:
        raise AppError("validation_error", 422, "Invalid retention set")
    if not revision_ids:
        return
    rows = (
        await session.execute(
            select(
                DocumentRevision.id,
                DocumentRevision.body.is_not(None),
                DocumentRevision.availability,
            )
            .where(DocumentRevision.id.in_(revision_ids))
            .order_by(DocumentRevision.id)
            .with_for_update()
        )
    ).all()
    keep = [
        identity
        for identity, has_body, availability in rows
        if has_body and availability == "available"
    ]
    for revision_id in keep:
        statement = pg_insert(RevisionRetention).values(
            owner_kind=owner_kind,
            owner_id=owner_id,
            revision_id=revision_id,
            expires_at=expires_at,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["owner_kind", "owner_id", "revision_id"],
                set_={
                    "expires_at": func.greatest(
                        RevisionRetention.expires_at, statement.excluded.expires_at
                    )
                },
            )
        )


class KnowledgeRetention:
    def __init__(self, factory):
        self.factory = factory

    async def sweep(self, lease, *, limit: int = CLEANUP_BATCH) -> RetentionResult:
        if type(limit) is not int or not 1 <= limit <= CLEANUP_BATCH:
            raise AppError("validation_error", 422, "Invalid cleanup limit")
        result = RetentionResult()
        # Binding hint only; it is rechecked under the owned Job lock below.
        async with self.factory() as hint:
            repository_id = await hint.scalar(
                select(Job.repository_id).where(Job.id == lease.job_id)
            )
        if repository_id is None:
            return result
        async with transaction(self.factory) as db:
            await db.execute(
                pg_insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
            )
            capacity = await db.get(KnowledgeCapacity, 1, with_for_update=True)
            await db.execute(
                pg_insert(RepositoryCapacity)
                .values(repository_id=repository_id)
                .on_conflict_do_nothing()
            )
            repository_capacity = await db.get(
                RepositoryCapacity, repository_id, with_for_update=True
            )
            job, now = await Queue(self.factory)._owned(db, lease)
            if job is None:
                return result
            if job.kind != "knowledge.cleanup" or job.repository_id != repository_id:
                raise AppError("validation_error", 422, "Not a cleanup job")
            payload = KnowledgeCleanupInput.model_validate(job.input)
            lease_expiry = job.lease_expires_at
            corpus = await db.get(KnowledgeCorpus, repository_id, with_for_update=True)

            batch = min(limit, payload.limit)
            ids = list(
                await db.scalars(
                    _candidate_ids(repository_id, payload.after).limit(batch)
                )
            )
            if not ids and payload.after is not None:
                result.wrapped = True
                ids = list(
                    await db.scalars(_candidate_ids(repository_id, None).limit(batch))
                )
            if not ids:
                result.cursor = None
                await self._finish(db, job, result)
                final_now = await db.scalar(select(func.clock_timestamp()))
                if final_now >= lease_expiry:
                    raise AppError("lease_expired", 409, "Cleanup lease expired")
                return result

            pairs = (
                await db.execute(
                    select(
                        DocumentRevision.id,
                        DocumentRevision.document_id,
                    ).where(DocumentRevision.id.in_(ids))
                )
            ).all()
            document_ids = sorted({pair.document_id for pair in pairs}, key=str)
            documents = {
                document.id: document
                for document in await db.scalars(
                    select(Document)
                    .where(Document.id.in_(document_ids))
                    .order_by(Document.id)
                    .with_for_update()
                )
            }
            revisions = list(
                await db.scalars(
                    select(DocumentRevision)
                    .options(defer(DocumentRevision.body))
                    .where(DocumentRevision.id.in_(ids))
                    .order_by(DocumentRevision.id)
                    .with_for_update()
                )
            )
            # Values are stable under the row locks above; body bytes are never
            # pulled into memory just to decide whether they may be cleared.
            with_body = set(
                await db.scalars(
                    select(DocumentRevision.id).where(
                        DocumentRevision.id.in_(ids),
                        DocumentRevision.body.is_not(None),
                    )
                )
            )

            protected = set(
                await db.scalars(
                    select(ImportMember.revision_id)
                    .join(
                        KnowledgeImport,
                        KnowledgeImport.id == ImportMember.import_id,
                    )
                    .where(
                        ImportMember.revision_id.in_(ids),
                        ImportMember.revision_id.is_not(None),
                        KnowledgeImport.status.in_(("queued", "building")),
                        KnowledgeImport.deadline > now,
                    )
                )
            )
            protected.update(
                await db.scalars(
                    select(UploadStage.revision_id).where(
                        UploadStage.revision_id.in_(ids),
                        UploadStage.consumed_import_id.is_(None),
                        UploadStage.expires_at > now,
                    )
                )
            )
            protected.update(
                await db.scalars(
                    select(RevisionRetention.revision_id).where(
                        RevisionRetention.revision_id.in_(ids),
                        RevisionRetention.expires_at > now,
                    )
                )
            )
            protected.update(
                await db.scalars(
                    select(RevisionPin.revision_id).where(
                        RevisionPin.revision_id.in_(ids),
                        RevisionPin.expires_at > now,
                    )
                )
            )
            if corpus is not None:
                protected.update(
                    await db.scalars(
                        select(ImportMember.revision_id)
                        .join(
                            KnowledgeImport,
                            KnowledgeImport.id == ImportMember.import_id,
                        )
                        .where(
                            ImportMember.revision_id.in_(ids),
                            ImportMember.revision_id.is_not(None),
                            ImportMember.status == "included",
                            KnowledgeImport.repository_id == repository_id,
                            KnowledgeImport.status.in_(("ready", "ready_partial")),
                            KnowledgeImport.composition_version == 1,
                            KnowledgeImport.target_generation
                            == corpus.target_generation,
                        )
                    )
                )

            scopes = dict(corpus.path_scopes or {}) if corpus is not None else {}
            for revision in revisions:
                document = documents.get(revision.document_id)
                if (
                    corpus is not None
                    and document is not None
                    and document.kind == "workspace"
                    and not document.deleted
                    and revision.workspace_id == corpus.target_workspace_id
                    and revision.sha == corpus.target_sha
                    and document.path is not None
                    and path_reason(document.path, **scopes) is None
                ):
                    protected.add(revision.id)
                if (
                    document is not None
                    and document.kind == "upload"
                    and not document.deleted
                    and document.accepted_revision_id == revision.id
                ):
                    protected.add(revision.id)

            for revision in revisions:
                result.examined += 1
                if revision.id in protected:
                    result.deferred += 1
                    result.protected.add(revision.id)
                    continue
                reclaimed = False
                if revision.id in with_body:
                    capacity.body_bytes -= revision.body_bytes
                    result.bytes_reclaimed += revision.body_bytes
                    revision.body = None
                    reclaimed = True
                if revision.availability == "available":
                    revision.availability = "expired"
                    reclaimed = True
                revision.searchable = False
                removed = len(
                    (
                        await db.execute(
                            delete(Chunk)
                            .where(Chunk.revision_id == revision.id)
                            .returning(Chunk.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if removed:
                    capacity.chunks -= removed
                    repository_capacity.chunks -= removed
                    result.chunks_reclaimed += removed
                    reclaimed = True
                if reclaimed:
                    result.reclaimed += 1

            result.cursor = revisions[-1].id
            await self._finish(db, job, result)

            final_now = await db.scalar(select(func.clock_timestamp()))
            if final_now >= lease_expiry:
                raise AppError("lease_expired", 409, "Cleanup lease expired")
            return result

    @staticmethod
    async def _finish(db, job: Job, result: RetentionResult):
        # The success interval is measured from completion, not sweep start.
        finished_at = await db.scalar(select(func.clock_timestamp()))
        job.status, job.error_code = "succeeded", None
        job.result = KnowledgeCleanupResult(
            examined=result.examined,
            reclaimed=result.reclaimed,
            deferred=result.deferred,
            bytes_reclaimed=result.bytes_reclaimed,
            chunks_reclaimed=result.chunks_reclaimed,
            wrapped=result.wrapped,
            cursor=result.cursor,
        ).model_dump(mode="json")
        job.lease_owner = job.lease_expires_at = None
        job.updated_at = finished_at
        return await Queue._transition(db, job, "job.succeeded")


class KnowledgeMaintenance:
    """Bounded rotating producer for ``knowledge.cleanup`` system jobs."""

    def __init__(self, factory):
        self.factory = factory
        self._after: UUID | None = None
        # Per-repository exact scan cursor; process-local and restart-safe
        # because it is re-seeded from the last successful Job result.
        self._scan_after: dict[UUID, UUID | None] = {}

    async def tick(self) -> int:
        async with self.factory() as db:
            query = select(RepositoryCapacity.repository_id)
            if self._after is not None:
                query = query.where(RepositoryCapacity.repository_id > self._after)
            repositories = list(
                await db.scalars(
                    query.order_by(RepositoryCapacity.repository_id).limit(
                        DISCOVERY_BATCH
                    )
                )
            )
            if not repositories and self._after is not None:
                self._after = None
                repositories = list(
                    await db.scalars(
                        select(RepositoryCapacity.repository_id)
                        .order_by(RepositoryCapacity.repository_id)
                        .limit(DISCOVERY_BATCH)
                    )
                )
        if not repositories:
            return 0
        self._after = repositories[-1]
        created = 0
        for repository_id in repositories:
            try:
                if await self._enqueue(repository_id):
                    created += 1
            except AppError:
                continue
        return created

    async def _enqueue(self, repository_id: UUID) -> bool:
        async with transaction(self.factory) as db:
            await db.execute(
                pg_insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
            )
            await db.get(KnowledgeCapacity, 1, with_for_update=True)
            await db.execute(
                pg_insert(RepositoryCapacity)
                .values(repository_id=repository_id)
                .on_conflict_do_nothing()
            )
            await db.get(RepositoryCapacity, repository_id, with_for_update=True)
            now = await db.scalar(select(func.clock_timestamp()))
            last = await db.scalar(
                select(Job)
                .where(
                    Job.repository_id == repository_id,
                    Job.kind == "knowledge.cleanup",
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
                .with_for_update()
            )
            if last is not None:
                if last.status in {"queued", "running", "retry_wait"}:
                    return False
                if last.status == "failed" and last.updated_at > now - CLEANUP_COOLDOWN:
                    return False
                if (
                    last.status == "succeeded"
                    and last.updated_at > now - CLEANUP_INTERVAL
                ):
                    return False
            succeeded = await db.scalar(
                select(Job)
                .where(
                    Job.repository_id == repository_id,
                    Job.kind == "knowledge.cleanup",
                    Job.status == "succeeded",
                )
                .order_by(Job.updated_at.desc(), Job.id.desc())
                .limit(1)
            )
            after = None
            if succeeded is not None and succeeded.result:
                cursor = succeeded.result.get("cursor")
                if cursor is not None:
                    after = UUID(cursor)
            corpus = await db.get(KnowledgeCorpus, repository_id, with_for_update=True)
            if repository_id not in self._scan_after:
                self._scan_after[repository_id] = after
            scan_after = self._scan_after[repository_id]
            found, next_scan = await _candidate_scan(
                db, repository_id, corpus, scan_after, now
            )
            if not found:
                self._scan_after[repository_id] = next_scan
                return False
            after = scan_after
            self._scan_after.pop(repository_id, None)
            value = KnowledgeCleanupInput(
                version=1, limit=CLEANUP_BATCH, after=after
            ).model_dump(mode="json")
            job = Job(
                kind="knowledge.cleanup",
                repository_id=repository_id,
                requested_by=None,
                input=value,
                input_hash=hashlib.sha256(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                idempotency_key=(
                    f"cleanup:{last.id.hex}" if last is not None else "cleanup:initial"
                ),
                available_at=now,
            )
            db.add(job)
            await db.flush()
            await append_event(db, job, "queued")
            await write_audit(
                db,
                AuditEntry(
                    action="job.submitted",
                    resource_type="job",
                    resource_id=job.id,
                    job_id=job.id,
                    outcome="success",
                ),
            )
            return True
