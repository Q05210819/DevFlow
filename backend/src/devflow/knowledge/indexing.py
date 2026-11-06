"""Versioned, atomically published knowledge indexes.

PostgreSQL owns the candidate identity, its expected chunk manifest and the
per-corpus active pointer. Milvus rows are keyed by index version, so a new
candidate can be built batch by batch without making a single vector visible
to queries until the whole manifest has been verified under the publication
fence. External model and Milvus calls always run outside the short
transactions; only their bookkeeping is fenced.
"""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import (
    MODEL_BACKGROUND_BATCH_CHUNKS,
    IdempotencyKey,
    KnowledgeIndexInput,
)
from devflow.jobs.service import append_event
from devflow.knowledge.chunking import CHUNKING_VERSION
from devflow.knowledge.milvus import (
    ANALYZER_VERSION,
    DIMENSION,
    SCHEMA_VERSION,
    IndexFingerprint,
    IndexRow,
)
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    IndexMember,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
)
from devflow.knowledge.schemas import (
    AvailabilityReason,
    AvailabilityStatus,
    IndexReceipt,
    RepositoryEpoch,
    SourceGrant,
)
from devflow.sync.scope import transaction

KEY = TypeAdapter(IdempotencyKey)

ACTIVE = "active"
TERMINAL = frozenset({"ready", "retired", "failed", "superseded"})
UNFINISHED = frozenset({"queued", "building"})
VISIBILITY_WINDOW = timedelta(seconds=60)
# One build owns a fixed 24-hour window, and its failures are cumulative across
# the successor Jobs that carry it batch by batch. A fresh Job never resets the
# count, so a failing build cannot retry forever.
BUILD_WINDOW = timedelta(hours=24)
SOURCE_ERRORS = frozenset(
    {
        "version_changed",
        "resource_not_found",
        "unauthenticated",
        "reauth_required",
        "forbidden",
    }
)


@dataclass(frozen=True, slots=True)
class VisibilityProof:
    """Evidence that the exact manifest was readable from Milvus recently.

    ``observed_at`` is a database clock reading taken after the batch
    bookkeeping commit, so an old proof cannot authorize a later publication.
    """

    index_id: UUID
    member_digest: str
    fingerprint: IndexFingerprint
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _Prepared:
    index_id: UUID
    repository_id: UUID
    fingerprint: IndexFingerprint
    items: tuple[tuple[UUID, str], ...]


def _changed():
    return AppError("version_changed", 409, "Knowledge target changed")


def _invalid():
    return AppError("validation_error", 422, "Invalid knowledge index request")


def _conflict():
    return AppError("idempotency_conflict", 409, "Idempotency key conflicts with input")


def _unavailable(message="Knowledge index backend unavailable"):
    return AppError("service_unavailable", 503, message)


def receipt(row: IndexVersion) -> IndexReceipt:
    return IndexReceipt(
        index_id=row.id,
        job_id=row.job_id,
        target_generation=row.target_generation,
    )


def _digest(rows) -> str:
    return hashlib.sha256(
        "".join(f"{chunk}:{ordinal}\n" for chunk, ordinal in rows).encode()
    ).hexdigest()


def _request_hash(imported: KnowledgeImport, actor: Actor) -> str:
    payload = {
        "import_id": str(imported.id),
        "repository_id": str(imported.repository_id),
        "target_generation": imported.target_generation,
        "target_sha": imported.target_sha,
        "path_rule_version": imported.path_rule_version,
        "target_path_scopes": imported.target_path_scopes,
        "actor_id": str(actor.id),
        "auth_version": actor.auth_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _authorization(grant: SourceGrant, now: datetime) -> dict:
    return {
        "actor": {
            "id": str(grant.actor.id),
            "github_id": grant.actor.github_id,
            "auth_version": grant.actor.auth_version,
            "role": grant.actor.role,
        },
        "repositories": [
            {
                "repository_id": str(epoch.repository_id),
                "installation_id": epoch.installation_id,
                "user_version": epoch.user_version,
                "installation_version": epoch.installation_version,
                "repository_version": epoch.repository_version,
            }
            for epoch in grant.repository_epochs
        ],
        "recorded_at": now.isoformat(),
    }


def _grant_from(value) -> SourceGrant | None:
    """Parse the stored authorization reference without trusting its shape."""
    try:
        actor = value["actor"]
        grant = SourceGrant(
            Actor(
                id=UUID(actor["id"]),
                github_id=int(actor["github_id"]),
                role=actor["role"],
                auth_version=int(actor["auth_version"]),
            ),
            tuple(
                RepositoryEpoch(
                    repository_id=UUID(item["repository_id"]),
                    installation_id=int(item["installation_id"]),
                    user_version=int(item["user_version"]),
                    installation_version=int(item["installation_version"]),
                    repository_version=int(item["repository_version"]),
                )
                for item in value["repositories"]
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not grant.repository_epochs:
        return None
    return grant


def fingerprint_from(value) -> IndexFingerprint | None:
    """Parse a stored JSON fingerprint; ``None`` means malformed, not empty."""
    if not value:
        return None
    try:
        return IndexFingerprint(**value)
    except (TypeError, ValueError):
        return None


# Storage probes stay bounded even for very large corpora. A larger index is
# checked for manifest completeness plus a fixed visible sample; the full
# manifest is still proven once at publication time.
PROBE_MEMBER_CAP = 256


@dataclass(frozen=True, slots=True)
class IndexAvailability:
    """Whether the current corpus target can be queried right now.

    ``ready`` is only true when the published pointer names the current target,
    its stored fingerprint matches the expected runtime, and (when explicitly
    verified) the bounded Milvus probe still sees the version. Every other state
    carries a stable reason so operators can distinguish "not built yet" from
    "built but the storage was restored incompletely".
    """

    status: AvailabilityStatus
    reason: AvailabilityReason | None = None
    index_id: UUID | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def require(self) -> None:
        if not self.ready:
            raise AppError(
                "index_not_ready",
                409,
                f"Knowledge index is not ready ({self.reason})",
            )


def evaluate_index_availability(
    corpus,
    active: IndexVersion | None,
    target: IndexVersion | None,
    *,
    fingerprint: IndexFingerprint | None = None,
) -> IndexAvailability:
    """Pure database-state evaluation; never calls Milvus."""
    if corpus is None or corpus.target_generation <= 0 or not corpus.target_sha:
        return IndexAvailability("not_indexed", "no_target")
    generation = corpus.target_generation
    active_ready = (
        active is not None
        and active.status == "ready"
        and active.queryable
        and active.purged_at is None
    )
    if active_ready and active.target_generation == generation:
        if (
            fingerprint is not None
            and fingerprint_from(active.fingerprint) != fingerprint
        ):
            return IndexAvailability("stale", "fingerprint_mismatch", active.id)
        return IndexAvailability("ready", None, active.id)
    if (
        target is not None
        and target.target_generation == generation
        and target.status in UNFINISHED
    ):
        return IndexAvailability("building", "target_building", target.id)
    if (
        target is not None
        and target.target_generation == generation
        and target.status == "failed"
    ):
        return IndexAvailability("stale", "target_failed", target.id)
    if active_ready:
        # The active version still serves an older, explicitly addressable SHA,
        # but the default current target has no published index.
        return IndexAvailability("stale", "target_pending", active.id)
    if active is not None:
        if active.purged_at is not None:
            reason: AvailabilityReason = "vectors_reclaimed"
        elif active.status != "ready" or not active.queryable:
            reason = "active_unavailable"
        else:
            reason = "fingerprint_mismatch"
        return IndexAvailability("stale", reason, active.id)
    return IndexAvailability("not_indexed", "target_not_indexed")


class IndexAvailabilityService:
    """Read-only readiness projection for queries and operational checks."""

    def __init__(self, factory, *, index=None):
        self.factory = factory
        self.index = index

    async def check(
        self,
        repository_id: UUID,
        *,
        fingerprint: IndexFingerprint | None = None,
        verify_storage: bool = False,
    ) -> IndexAvailability:
        async with self.factory() as db:
            corpus = await db.get(KnowledgeCorpus, repository_id)
            active = None
            target = None
            if corpus is not None:
                if corpus.active_index_version_id is not None:
                    active = await db.get(IndexVersion, corpus.active_index_version_id)
                if corpus.target_generation > 0:
                    target = await db.scalar(
                        select(IndexVersion)
                        .where(
                            IndexVersion.repository_id == repository_id,
                            IndexVersion.target_generation == corpus.target_generation,
                        )
                        .order_by(
                            IndexVersion.created_at.desc(), IndexVersion.id.desc()
                        )
                        .limit(1)
                    )
            result = evaluate_index_availability(
                corpus, active, target, fingerprint=fingerprint
            )
        if not verify_storage or self.index is None or not result.ready:
            return result
        return await self.verify(result.index_id)

    async def verify(self, index_id: UUID) -> IndexAvailability:
        """Verify one published version against Milvus without creating anything.

        The exact version-scoped row count is compared before a bounded sample
        of member IDs is read, so losing a vector outside the sample cannot
        still report ready. Zero-member manifests still require the collection
        to exist and to contain exactly zero rows for this version.
        """
        if self.index is None:
            raise RuntimeError("storage verification requires a Milvus adapter")
        index_id = index_id if isinstance(index_id, UUID) else UUID(str(index_id))
        async with self.factory() as db:
            active = await db.get(IndexVersion, index_id)
            if active is None:
                return IndexAvailability("stale", "active_unavailable", index_id)
            if active.purged_at is not None:
                return IndexAvailability("stale", "vectors_reclaimed", index_id)
            if active.status != "ready" or not active.queryable:
                return IndexAvailability("stale", "active_unavailable", index_id)
            expected = active.expected_member_count
            written = active.written_count
            members = list(
                await db.scalars(
                    select(IndexMember.chunk_id)
                    .where(
                        IndexMember.index_version_id == index_id,
                        IndexMember.status == "written",
                    )
                    .order_by(IndexMember.ordinal)
                    .limit(PROBE_MEMBER_CAP)
                )
            )
            stored = fingerprint_from(active.fingerprint)
        if written != expected or len(members) != min(expected, PROBE_MEMBER_CAP):
            return IndexAvailability("stale", "vectors_missing", index_id)
        if stored is None:
            return IndexAvailability("stale", "fingerprint_mismatch", index_id)
        reason = await asyncio.to_thread(
            self.index.probe,
            stored,
            index_id,
            members,
            expected_count=expected,
        )
        if reason is None:
            return IndexAvailability("ready", None, index_id)
        return IndexAvailability("stale", reason, index_id)


def _same_scope(left, right) -> bool:
    return dict(left or {}) == dict(right or {})


def _same_target(
    *,
    generation,
    sha,
    path_rule_version,
    path_scopes,
    row: IndexVersion,
) -> bool:
    return (
        generation == row.target_generation
        and sha == row.target_sha
        and path_rule_version == row.path_rule_version
        and _same_scope(path_scopes, row.path_scopes)
    )


def _corpus_matches(corpus: KnowledgeCorpus | None, imported: KnowledgeImport) -> bool:
    if (
        corpus is None
        or imported.composition_version != 1
        or imported.status not in {"ready", "ready_partial"}
    ):
        return False
    return (
        corpus.target_generation == imported.target_generation
        and corpus.target_sha == imported.target_sha
        and corpus.path_rule_version == imported.path_rule_version
        and _same_scope(corpus.path_scopes, imported.target_path_scopes)
    )


def _target_matches(
    corpus: KnowledgeCorpus | None, imported: KnowledgeImport | None, row: IndexVersion
) -> bool:
    if (
        corpus is None
        or imported is None
        or imported.repository_id != row.repository_id
        or imported.id != row.import_id
        or imported.composition_version != 1
        or imported.status not in {"ready", "ready_partial"}
    ):
        return False
    return _same_target(
        generation=imported.target_generation,
        sha=imported.target_sha,
        path_rule_version=imported.path_rule_version,
        path_scopes=imported.target_path_scopes,
        row=row,
    ) and _same_target(
        generation=corpus.target_generation,
        sha=corpus.target_sha,
        path_rule_version=corpus.path_rule_version,
        path_scopes=corpus.path_scopes,
        row=row,
    )


async def _manifest(db, imported: KnowledgeImport) -> list[Chunk]:
    """The exact immutable chunk set an index of this import promises.

    Mirrors ``ingestion.completed_chunks``: the same selection rules, member
    bookkeeping and count are the only source of manifest membership.
    """
    invalid = await db.scalar(
        select(ImportMember.id)
        .outerjoin(DocumentRevision, DocumentRevision.id == ImportMember.revision_id)
        .outerjoin(Document, Document.id == DocumentRevision.document_id)
        .where(
            ImportMember.import_id == imported.id,
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
        raise _changed()
    expected, member_count = (
        await db.execute(
            select(
                func.coalesce(func.sum(ImportMember.total_chunks), 0),
                func.count(),
            ).where(
                ImportMember.import_id == imported.id,
                ImportMember.status == "included",
            )
        )
    ).one()
    if member_count != imported.included or await db.scalar(
        select(ImportMember.id)
        .where(
            ImportMember.import_id == imported.id,
            ImportMember.status == "included",
            (
                ImportMember.total_chunks.is_(None)
                | (ImportMember.next_chunk != ImportMember.total_chunks)
            ),
        )
        .limit(1)
    ):
        raise _changed()
    query = (
        select(Chunk)
        .join(ImportMember, ImportMember.revision_id == Chunk.revision_id)
        .join(DocumentRevision, Chunk.revision_id == DocumentRevision.id)
        .join(Document, Document.id == DocumentRevision.document_id)
        .where(
            ImportMember.import_id == imported.id,
            ImportMember.status == "included",
            ImportMember.next_chunk == ImportMember.total_chunks,
            Chunk.ordinal < ImportMember.total_chunks,
            Chunk.tokenizer_fingerprint == imported.tokenizer_fingerprint,
            Chunk.chunking_version == imported.chunking_version,
            Document.deleted.is_(False),
            DocumentRevision.searchable.is_(True),
            DocumentRevision.availability == "available",
            DocumentRevision.published_at.is_not(None),
        )
    )
    actual = await db.scalar(select(func.count()).select_from(query.subquery()))
    if actual != expected:
        raise _changed()
    return list(await db.scalars(query.order_by(Chunk.id)))


def _holds(job: Job | None, lease) -> bool:
    return (
        job is not None
        and job.lease_owner == lease.owner
        and job.lease_generation == lease.generation
        and job.attempt == lease.attempt
        and job.status == "running"
    )


def _alive(job: Job | None, lease, now: datetime) -> bool:
    return (
        _holds(job, lease)
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
    )


async def _locked_job(db, lease) -> tuple[Job | None, datetime]:
    job = await db.scalar(select(Job).where(Job.id == lease.job_id).with_for_update())
    now = await db.scalar(select(func.clock_timestamp()))
    return job, now


def _payload(job: Job | None) -> KnowledgeIndexInput | None:
    if job is None:
        return None
    try:
        return KnowledgeIndexInput.model_validate(job.input)
    except ValidationError:
        return None


async def _supersede_rows(db, rows, now: datetime | None = None) -> None:
    """Terminalize only unfinished candidates and their batch Jobs.

    Published versions (ready/retired) are history and must never be rewritten
    by a newer request. The caller already holds the candidate row locks; its
    related Jobs are locked with ``SKIP LOCKED`` so a worker that currently
    owns one can never be waited on while it waits for a corpus or candidate
    lock held here. A skipped Job stays recoverable, sees the superseded row at
    its next fence, and terminalizes itself there.
    """
    candidates = [row for row in rows if row is not None and row.status in UNFINISHED]
    if not candidates:
        return
    now = now or await db.scalar(select(func.clock_timestamp()))
    ids = [row.id for row in candidates]
    job_ids = [row.job_id for row in candidates]
    for row in candidates:
        row.status = "superseded"
        row.queryable = False
        row.updated_at = now
    jobs = list(
        await db.scalars(
            select(Job)
            .where(
                (Job.id.in_(job_ids))
                | (
                    (Job.kind == "knowledge.index_batch")
                    & (Job.input["index_id"].astext.in_([str(value) for value in ids]))
                )
            )
            .order_by(Job.id)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        if job.status in {"queued", "running", "retry_wait"}:
            job.status = "succeeded"
            job.result = {"status": "superseded"}
            job.error_code = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            await Queue._transition(db, job, "job.succeeded")


async def _finish_job(db, job: Job, now: datetime, status: str) -> None:
    job.status = "succeeded"
    job.result = {"status": status}
    job.error_code = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    await Queue._transition(db, job, "job.succeeded")


async def _fail_job(db, job: Job, now: datetime, code: str) -> None:
    """Fail one batch Job whose candidate can no longer make progress."""

    job.status = "failed"
    job.result = None
    job.error_code = code
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now
    await Queue._transition(db, job, "job.failed")


def _expire_build(row: IndexVersion, now: datetime) -> bool:
    """Mark a candidate failed once its fixed build window has elapsed."""

    if now < row.deadline:
        return False
    row.status = "failed"
    row.queryable = False
    row.updated_at = now
    return True


class IndexBuilder:
    """Request, build and publish immutable indexes for one corpus target."""

    def __init__(self, service, *, inference=None, index=None):
        self.service = service
        self.inference = inference
        self.index = index

    async def request(
        self,
        actor: Actor,
        import_id: UUID,
        idempotency_key: str,
        *,
        repository_id: UUID | None = None,
    ) -> IndexReceipt:
        if actor.role != "admin":
            raise AppError("forbidden", 403, "Administrator access required")
        try:
            key = KEY.validate_python(idempotency_key)
        except (ValidationError, ValueError):
            raise _invalid() from None
        async with self.service.factory() as db:
            imported = await db.get(KnowledgeImport, import_id)
            if imported is None or (
                repository_id is not None and imported.repository_id != repository_id
            ):
                raise not_found()
            dependencies = tuple(
                await db.scalars(
                    select(ImportRepositoryEpoch.repository_id).where(
                        ImportRepositoryEpoch.import_id == imported.id
                    )
                )
            )
            if not dependencies:
                raise not_found()
            repository = imported.repository_id
            digest = _request_hash(imported, actor)
        grant = await self.service.access.verify(actor, dependencies, fresh=True)
        async with transaction(self.service.factory) as db:
            await self.service._capacity(db, repository)
            await self.service.access.lock(db, grant)
            await self.service._admin(db, grant.actor)
            await self.service._initialize_repository_capacity(db, repository)
            existing = await db.scalar(
                select(IndexVersion).where(
                    IndexVersion.repository_id == repository,
                    IndexVersion.idempotency_key == key,
                )
            )
            if existing is not None:
                if existing.request_hash != digest:
                    raise _conflict()
                original = await db.get(
                    KnowledgeImport,
                    existing.import_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if original is None:
                    raise not_found()
                await self.service._original_epochs(db, original, grant)
                return receipt(existing)
            corpus = await db.get(
                KnowledgeCorpus,
                repository,
                with_for_update=True,
                populate_existing=True,
            )
            row = await db.get(
                KnowledgeImport, import_id, with_for_update=True, populate_existing=True
            )
            if row is None:
                raise not_found()
            await self.service._original_epochs(db, row, grant)
            if not _corpus_matches(corpus, row):
                raise _changed()
            chunks = await _manifest(db, row)
            now = await db.scalar(select(func.clock_timestamp()))
            older = list(
                await db.scalars(
                    select(IndexVersion)
                    .where(
                        IndexVersion.repository_id == repository,
                        IndexVersion.status.in_(UNFINISHED),
                    )
                    .order_by(IndexVersion.created_at, IndexVersion.id)
                    .with_for_update()
                )
            )
            await _supersede_rows(db, older, now)
            identity = uuid4()
            payload = KnowledgeIndexInput(version=1, index_id=identity)
            job = Job(
                id=uuid4(),
                kind="knowledge.index_batch",
                repository_id=repository,
                requested_by=actor.id,
                input=payload.model_dump(mode="json"),
                input_hash=digest,
                idempotency_key=key,
                max_attempts=3,
                available_at=now,
            )
            db.add(job)
            await db.flush()
            version = IndexVersion(
                id=identity,
                repository_id=repository,
                import_id=row.id,
                job_id=job.id,
                requested_by=actor.id,
                target_generation=row.target_generation,
                target_sha=row.target_sha,
                path_rule_version=row.path_rule_version,
                path_scopes=dict(row.target_path_scopes or {}),
                idempotency_key=key,
                request_hash=digest,
                authorization=_authorization(grant, now),
                status="queued",
                fingerprint={},
                expected_member_count=len(chunks),
                written_count=0,
                member_digest=_digest(
                    (chunk.id, ordinal) for ordinal, chunk in enumerate(chunks)
                ),
                queryable=False,
                created_at=now,
                deadline=now + BUILD_WINDOW,
                failures=0,
                updated_at=now,
            )
            db.add(version)
            await db.flush()
            for ordinal, chunk in enumerate(chunks):
                db.add(
                    IndexMember(
                        id=uuid4(),
                        index_version_id=identity,
                        chunk_id=chunk.id,
                        ordinal=ordinal,
                        status="pending",
                    )
                )
            await db.flush()
            await append_event(db, job, "queued")
            await write_audit(
                db,
                AuditEntry(
                    actor_id=actor.id,
                    action="job.submitted",
                    resource_type="job",
                    resource_id=job.id,
                    job_id=job.id,
                    outcome="success",
                ),
            )
            return receipt(version)

    async def run_batch(self, lease) -> None:
        if self.inference is None or self.index is None:
            raise _unavailable()
        model = await self.inference.start()
        if model.dimension != DIMENSION:
            raise _unavailable("Embedding dimension does not match the index schema")
        prepared = await self._prepare(lease, model)
        if prepared is None:
            return
        if prepared.items:
            await asyncio.to_thread(self.index.ensure_schema, prepared.fingerprint)
            texts = [text for _, text in prepared.items]
            vectors = await self.inference.embed(texts)
            if len(vectors) != len(texts):
                raise _unavailable("Embedding batch returned the wrong size")
            rows = [
                IndexRow(
                    chunk_id=chunk_id,
                    repository_id=prepared.repository_id,
                    text=text,
                    dense=vector,
                )
                for (chunk_id, text), vector in zip(prepared.items, vectors)
            ]
            await asyncio.to_thread(self.index.upsert, prepared.index_id, rows)
        recorded = await _record_batch(
            self.service.factory,
            lease,
            prepared.index_id,
            [chunk_id for chunk_id, _ in prepared.items],
        )
        if recorded is not True:
            return
        async with self.service.factory() as db:
            row = await db.get(IndexVersion, prepared.index_id)
            if row is None:
                return
            index_id = row.id
            member_digest = row.member_digest
            member_ids = list(
                await db.scalars(
                    select(IndexMember.chunk_id)
                    .where(IndexMember.index_version_id == index_id)
                    .order_by(IndexMember.ordinal)
                )
            )
        visible = await asyncio.to_thread(
            self.index.verify_visible, index_id, member_ids
        )
        if not visible:
            raise _unavailable("Indexed vectors are not visible")
        # The proof must be observed after the visibility scan, otherwise a
        # slow scan leaves a proof that was already stale when it was created.
        async with self.service.factory() as db:
            observed = await db.scalar(select(func.clock_timestamp()))
        proof = VisibilityProof(
            index_id=index_id,
            member_digest=member_digest,
            fingerprint=prepared.fingerprint,
            observed_at=observed,
        )
        await self.publish(lease, index_id, proof)

    async def _prepare(self, lease, model) -> _Prepared | None:
        async with transaction(self.service.factory) as db:
            job, now = await _locked_job(db, lease)
            if not _alive(job, lease, now):
                return None
            payload = _payload(job)
            if (
                job.kind != "knowledge.index_batch"
                or job.repository_id is None
                or payload is None
            ):
                return None
            row = await db.get(
                IndexVersion,
                payload.index_id,
                with_for_update=True,
                populate_existing=True,
            )
            if row is None or row.repository_id != job.repository_id:
                return None
            if row.status == "failed":
                await _fail_job(db, job, now, "index_failed")
                return None
            if row.status in TERMINAL:
                await _finish_job(db, job, now, row.status)
                return None
            if _expire_build(row, now):
                await _fail_job(db, job, now, "build_deadline_exceeded")
                return None
            corpus = await db.get(KnowledgeCorpus, row.repository_id)
            imported = await db.get(KnowledgeImport, row.import_id)
            if not _target_matches(corpus, imported, row):
                await _supersede_rows(db, [row], now)
                return None
            built = IndexFingerprint(
                embedding=model.embedding,
                tokenizer=model.tokenizer,
                analyzer=ANALYZER_VERSION,
                chunking=imported.chunking_version or CHUNKING_VERSION,
                schema=SCHEMA_VERSION,
            )
            stored = fingerprint_from(row.fingerprint)
            if row.fingerprint and stored is None:
                await _supersede_rows(db, [row], now)
                return None
            if stored is not None and stored != built:
                await _supersede_rows(db, [row], now)
                return None
            if stored is None:
                row.fingerprint = asdict(built)
            if row.status == "queued":
                row.status = "building"
            row.updated_at = now
            pending = list(
                await db.scalars(
                    select(IndexMember)
                    .where(
                        IndexMember.index_version_id == row.id,
                        IndexMember.status == "pending",
                    )
                    .order_by(IndexMember.ordinal)
                    .limit(MODEL_BACKGROUND_BATCH_CHUNKS)
                    .with_for_update()
                )
            )
            by_id = {
                chunk.id: chunk
                for chunk in await db.scalars(
                    select(Chunk).where(
                        Chunk.id.in_([item.chunk_id for item in pending])
                    )
                )
            }
            items = []
            for member in pending:
                chunk = by_id.get(member.chunk_id)
                if chunk is None or not chunk.embedding_text:
                    await _supersede_rows(db, [row], now)
                    return None
                items.append((member.chunk_id, chunk.embedding_text))
            return _Prepared(
                index_id=row.id,
                repository_id=row.repository_id,
                fingerprint=built,
                items=tuple(items),
            )

    async def publish(self, lease, index_id: UUID, proof: VisibilityProof) -> bool:
        async with self.service.factory() as db:
            row = await db.get(IndexVersion, index_id)
            if (
                row is None
                or row.status in TERMINAL
                or not isinstance(proof, VisibilityProof)
            ):
                return False
            stored = _grant_from(row.authorization)
            repository_id = row.repository_id
            import_id = row.import_id
        if stored is None or repository_id not in {
            epoch.repository_id for epoch in stored.repository_epochs
        }:
            await self._supersede(lease, index_id)
            return False
        try:
            fresh = await self.service.access.verify(
                stored.actor,
                tuple(epoch.repository_id for epoch in stored.repository_epochs),
                fresh=True,
            )
        except AppError as error:
            if error.status in {401, 403, 404}:
                await self._supersede(lease, index_id)
                return False
            raise
        if (
            fresh.actor != stored.actor
            or fresh.repository_epochs != stored.repository_epochs
        ):
            await self._supersede(lease, index_id)
            return False
        try:
            return await self._publish_fenced(
                lease, index_id, import_id, repository_id, proof, fresh
            )
        except AppError as error:
            if error.code in SOURCE_ERRORS:
                await self._supersede(lease, index_id)
                return False
            raise

    async def _publish_fenced(
        self, lease, index_id, import_id, repository_id, proof, grant
    ) -> bool:
        async with transaction(self.service.factory) as db:
            await self.service._capacity(db, repository_id)
            await self.service.access.lock(db, grant)
            await self.service._admin(db, grant.actor)
            await self.service._initialize_repository_capacity(db, repository_id)
            job, now = await _locked_job(db, lease)
            corpus = await db.get(
                KnowledgeCorpus,
                repository_id,
                with_for_update=True,
                populate_existing=True,
            )
            imported = await db.get(
                KnowledgeImport,
                import_id,
                with_for_update=True,
                populate_existing=True,
            )
            row = await db.get(
                IndexVersion,
                index_id,
                with_for_update=True,
                populate_existing=True,
            )
            if row is None:
                return False
            if not _holds(job, lease):
                # A recovered owner (or a newer Job for this candidate) owns
                # the next move. A stale executor must reject only itself and
                # must never terminalize the candidate or its successor Jobs.
                return False
            if row.status == "failed":
                await _fail_job(db, job, now, "index_failed")
                return False
            if row.status in TERMINAL:
                return False
            if _expire_build(row, now):
                await _fail_job(db, job, now, "build_deadline_exceeded")
                return False
            if imported is None:
                await _supersede_rows(db, [row], now)
                return False
            if job.kind != "knowledge.index_batch":
                await _supersede_rows(db, [row], now)
                return False
            payload = _payload(job)
            if payload is None or payload.index_id != row.id:
                await _supersede_rows(db, [row], now)
                return False
            if job.lease_expires_at is None or job.lease_expires_at <= now:
                # Expiry alone is not evidence of supersession: the Job stays
                # recoverable and the candidate stays unpublished.
                return False
            if job.repository_id != repository_id:
                await _supersede_rows(db, [row], now)
                return False
            await self.service._original_epochs(db, imported, grant)
            if not _target_matches(corpus, imported, row):
                await _supersede_rows(db, [row], now)
                return False
            stored = fingerprint_from(row.fingerprint)
            if (
                stored is None
                or proof.index_id != row.id
                or proof.member_digest != row.member_digest
                or not isinstance(proof.fingerprint, IndexFingerprint)
                or proof.fingerprint != stored
                or not isinstance(proof.observed_at, datetime)
            ):
                await _supersede_rows(db, [row], now)
                return False
            observed = proof.observed_at
            if observed.tzinfo is None:
                await _supersede_rows(db, [row], now)
                return False
            delta = now - observed
            if delta < -VISIBILITY_WINDOW / 2 or delta > VISIBILITY_WINDOW:
                await _supersede_rows(db, [row], now)
                return False
            chunks = await _manifest(db, imported)
            if len(chunks) != row.expected_member_count:
                await _supersede_rows(db, [row], now)
                return False
            if (
                _digest((chunk.id, ordinal) for ordinal, chunk in enumerate(chunks))
                != row.member_digest
            ):
                await _supersede_rows(db, [row], now)
                return False
            members = list(
                await db.scalars(
                    select(IndexMember)
                    .where(IndexMember.index_version_id == row.id)
                    .order_by(IndexMember.ordinal)
                    .with_for_update()
                )
            )
            if (
                len(members) != row.expected_member_count
                or any(member.status != "written" for member in members)
                or row.written_count != row.expected_member_count
                or _digest((member.chunk_id, member.ordinal) for member in members)
                != row.member_digest
            ):
                await _supersede_rows(db, [row], now)
                return False
            # Manifest validation can outlive both the lease and the proof above.
            # Re-read the database clock and never publish on a stale fence.
            final_now = await db.scalar(select(func.clock_timestamp()))
            if not _alive(job, lease, final_now):
                return False
            if _expire_build(row, final_now):
                await _fail_job(db, job, final_now, "build_deadline_exceeded")
                return False
            if (
                proof.observed_at > final_now + VISIBILITY_WINDOW / 2
                or final_now - proof.observed_at > VISIBILITY_WINDOW
            ):
                return False
            now = final_now
            previous = None
            if (
                corpus.active_index_version_id is not None
                and corpus.active_index_version_id != row.id
            ):
                previous = await db.get(
                    IndexVersion,
                    corpus.active_index_version_id,
                    with_for_update=True,
                    populate_existing=True,
                )
            row.status = "ready"
            row.queryable = True
            row.published_at = row.published_at or now
            row.updated_at = now
            if previous is not None and previous.status == "ready":
                previous.status = "retired"
                previous.queryable = False
                previous.retired_at = previous.retired_at or now
                previous.updated_at = now
            corpus.active_index_version_id = row.id
            job.status = "succeeded"
            job.result = {"index_id": str(row.id), "status": "ready"}
            job.error_code = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            await Queue._transition(db, job, "job.succeeded")
            return True

    async def _supersede(self, lease, index_id: UUID) -> None:
        async with transaction(self.service.factory) as db:
            job, now = await _locked_job(db, lease)
            payload = _payload(job)
            if (
                not _holds(job, lease)
                or job.kind != "knowledge.index_batch"
                or payload is None
                or payload.index_id != index_id
            ):
                # The lease no longer owns this candidate. A newer owner will
                # re-check authorization and either publish or supersede it.
                return
            row = await db.get(
                IndexVersion,
                index_id,
                with_for_update=True,
                populate_existing=True,
            )
            if row is None:
                return
            await _supersede_rows(db, [row], now)


async def _record_batch(factory, lease, index_id: UUID, chunk_ids) -> bool | None:
    """Commit one batch's member bookkeeping after its external writes.

    Returns ``True`` when the whole manifest is written and publication may be
    attempted, ``False`` when a successor Job was queued, and ``None`` when the
    lease or candidate moved on. The caller's external upsert is idempotent, so
    a lost bookkeeping transaction simply replays the same batch.
    """
    async with transaction(factory) as db:
        job, now = await _locked_job(db, lease)
        if not _alive(job, lease, now):
            return None
        payload = _payload(job)
        if (
            job.kind != "knowledge.index_batch"
            or payload is None
            or payload.index_id != index_id
            or job.repository_id is None
        ):
            return None
        row = await db.get(
            IndexVersion,
            index_id,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None or row.repository_id != job.repository_id:
            return None
        if row.status == "failed":
            await _fail_job(db, job, now, "index_failed")
            return None
        if row.status not in UNFINISHED:
            return None
        if _expire_build(row, now):
            await _fail_job(db, job, now, "build_deadline_exceeded")
            return None
        if row.status == "queued":
            row.status = "building"
        row.updated_at = now
        for chunk_id in set(chunk_ids):
            member = await db.scalar(
                select(IndexMember)
                .where(
                    IndexMember.index_version_id == row.id,
                    IndexMember.chunk_id == chunk_id,
                    IndexMember.status == "pending",
                )
                .with_for_update()
            )
            if member is not None:
                member.status = "written"
                member.written_at = now
        written = await db.scalar(
            select(func.count())
            .select_from(IndexMember)
            .where(
                IndexMember.index_version_id == row.id,
                IndexMember.status == "written",
            )
        )
        row.written_count = int(written or 0)
        pending = await db.scalar(
            select(IndexMember.id)
            .where(
                IndexMember.index_version_id == row.id,
                IndexMember.status == "pending",
            )
            .limit(1)
        )
        if pending is None:
            return True
        successor = Job(
            id=uuid4(),
            kind="knowledge.index_batch",
            repository_id=row.repository_id,
            requested_by=row.requested_by,
            input=KnowledgeIndexInput(version=1, index_id=row.id).model_dump(
                mode="json"
            ),
            input_hash=row.request_hash,
            idempotency_key=uuid4().hex,
            max_attempts=3,
            available_at=now,
        )
        db.add(successor)
        await db.flush()
        await Queue._transition(db, successor, "job.submitted")
        await _finish_job(db, job, now, "building")
        return False


async def enqueue_index_for_import(db, imported: KnowledgeImport, grant, now) -> None:
    """Queue the same-generation index build when an import composes.

    Called inside the fenced import publication transaction. The corpus row is
    locked first; any older unfinished candidate is superseded in the same
    transaction so the database invariant of at most one unfinished candidate
    per repository holds at commit. Related Jobs are locked with ``SKIP LOCKED``
    so a worker that owns one is never waited on from here.
    """
    if imported.composition_version != 1 or imported.status not in {
        "ready",
        "ready_partial",
    }:
        return
    corpus = await db.get(KnowledgeCorpus, imported.repository_id, with_for_update=True)
    if not _corpus_matches(corpus, imported):
        return
    key = "auto-" + hashlib.sha256(f"index:{imported.id}".encode()).hexdigest()[:40]
    existing = await db.scalar(
        select(IndexVersion.id).where(
            IndexVersion.repository_id == imported.repository_id,
            IndexVersion.idempotency_key == key,
        )
    )
    if existing is not None:
        return
    # Composing a new target makes any older unfinished candidate obsolete.
    # Supersede it under the corpus lock so only one build can be in progress.
    older = list(
        await db.scalars(
            select(IndexVersion)
            .where(
                IndexVersion.repository_id == imported.repository_id,
                IndexVersion.status.in_(UNFINISHED),
            )
            .order_by(IndexVersion.created_at, IndexVersion.id)
            .with_for_update()
        )
    )
    await _supersede_rows(db, older, now)
    chunks = await _manifest(db, imported)
    identity = uuid4()
    version = IndexVersion(
        id=identity,
        repository_id=imported.repository_id,
        import_id=imported.id,
        job_id=uuid4(),
        requested_by=imported.actor_id,
        target_generation=imported.target_generation,
        target_sha=imported.target_sha,
        path_rule_version=imported.path_rule_version,
        path_scopes=dict(imported.target_path_scopes or {}),
        idempotency_key=key,
        request_hash=imported.request_hash,
        authorization=_authorization(grant, now),
        status="queued",
        fingerprint={},
        expected_member_count=len(chunks),
        written_count=0,
        member_digest=_digest(
            (chunk.id, ordinal) for ordinal, chunk in enumerate(chunks)
        ),
        queryable=False,
        created_at=now,
        deadline=now + BUILD_WINDOW,
        failures=0,
        updated_at=now,
    )
    job = Job(
        id=version.job_id,
        kind="knowledge.index_batch",
        repository_id=imported.repository_id,
        requested_by=imported.actor_id,
        input=KnowledgeIndexInput(version=1, index_id=identity).model_dump(mode="json"),
        input_hash=imported.request_hash,
        idempotency_key=key,
        max_attempts=3,
        available_at=now,
    )
    db.add(job)
    await db.flush()
    db.add(version)
    await db.flush()
    for ordinal, chunk in enumerate(chunks):
        db.add(
            IndexMember(
                id=uuid4(),
                index_version_id=identity,
                chunk_id=chunk.id,
                ordinal=ordinal,
                status="pending",
            )
        )
    await db.flush()
    await Queue._transition(db, job, "job.submitted")


__all__ = [
    "IndexBuilder",
    "VisibilityProof",
    "enqueue_index_for_import",
    "fingerprint_from",
]
