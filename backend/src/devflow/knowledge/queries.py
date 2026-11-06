"""Bounded read projections plus creator-private knowledge queries.

Metadata queries never load ``DocumentRevision.body``. Every page is bound to
the observed corpus generation and to a signed cursor purpose, so a stale page
cannot silently describe a newer selection. A query is durable only through its
Job and its bounded retained payload; nothing here executes model work inline.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import defer

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import User
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import IdempotencyKey
from devflow.jobs.service import append_event
from devflow.knowledge.access import KnowledgeAccess
from devflow.knowledge.indexing import (
    IndexAvailabilityService,
    evaluate_index_availability,
)
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    ImportMember,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
    KnowledgeQuery,
    KnowledgeQueryRepository,
    RevisionDependency,
    RevisionRetention,
)
from devflow.knowledge.retention import retain_sources
from devflow.knowledge.retrieval import RetrievalContext
from devflow.knowledge.schemas import (
    CorpusState,
    DocumentPage,
    DocumentSummary,
    DocumentView,
    EvidenceManifest,
    EvidenceView,
    KnowledgeView,
    PathEntry,
    PathPage,
    QueryReceipt,
    QueryResult,
    QuerySelection,
    QueryView,
    RevisionSummary,
    ScanLimits,
    SourceIdentity,
    metadata_bytes,
)
from devflow.knowledge.sources import PATH_RULE_VERSION, path_reason
from devflow.repositories.policy import check_actor
from devflow.sync.scope import transaction
from devflow.workspace.models import WorkspaceVersion

LIMITS = ScanLimits(max_files=2000, max_scan_bytes=33554432, max_file_bytes=1048576)
REVISION_PAGE = 30
QUERY_LIMIT = 20
QUESTION_LIMIT = 2048
MANIFEST_LIMIT = 65536
RESULT_LIMIT = 131072
RETENTION = timedelta(hours=1)
RETRIEVAL_DEADLINE = timedelta(seconds=20)
_QUERY_KIND = "knowledge.query"
_TERMINAL = ("succeeded", "failed")
_KEY = TypeAdapter(IdempotencyKey)


def changed():
    return AppError("version_changed", 409, "Knowledge selection changed")


def invalid_cursor():
    return AppError("invalid_cursor", 400, "Invalid cursor")


def _snapshot(corpus):
    if corpus is None:
        return (0, None, None, {})
    return (
        corpus.target_generation,
        corpus.target_workspace_id,
        corpus.target_sha,
        corpus.path_scopes,
    )


async def _dependencies(db, repository_id, workspace_id):
    """Repository ids whose grants a reader of this selection must hold."""
    dependencies = {repository_id}
    if workspace_id is not None:
        version = await db.get(WorkspaceVersion, workspace_id)
        if version is not None:
            dependencies |= {UUID(scope["repository_id"]) for scope in version.scopes}
    return dependencies


def _scope_digest(document_paths, code_paths):
    return hashlib.sha256(
        json.dumps(
            [list(document_paths), list(code_paths)],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _filename(document):
    if document.kind != "upload":
        return None
    value = (document.attributes or {}).get("filename")
    return value if isinstance(value, str) else None


async def _published_summaries(db, document_ids):
    """Counts plus the newest published revision per document, no body column.

    Published revisions keep their identity after reclamation; availability is
    reported per revision, so reclaimed rows still count and can be inspected.
    """
    if not document_ids:
        return {}
    published = (DocumentRevision.published_at.is_not(None),)
    counts = dict(
        (
            await db.execute(
                select(DocumentRevision.document_id, func.count())
                .where(DocumentRevision.document_id.in_(document_ids), *published)
                .group_by(DocumentRevision.document_id)
            )
        ).all()
    )
    ranked = (
        select(
            DocumentRevision.document_id.label("document_id"),
            DocumentRevision.id.label("revision_id"),
            DocumentRevision.published_at.label("published_at"),
            func.row_number()
            .over(
                partition_by=DocumentRevision.document_id,
                order_by=(
                    DocumentRevision.published_at.desc(),
                    DocumentRevision.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(DocumentRevision.document_id.in_(document_ids), *published)
        .subquery()
    )
    latest = {
        row.document_id: row
        for row in (
            await db.execute(
                select(
                    ranked.c.document_id, ranked.c.revision_id, ranked.c.published_at
                ).where(ranked.c.rank == 1)
            )
        ).all()
    }
    return {
        document_id: (counts.get(document_id, 0), latest.get(document_id))
        for document_id in document_ids
    }


async def _document_page(
    db, repository_id, dependencies, generation, *, limit, position
):
    published = (
        select(DocumentRevision.id)
        .where(
            DocumentRevision.document_id == Document.id,
            DocumentRevision.published_at.is_not(None),
        )
        .exists()
    )
    query = select(Document).where(
        Document.repository_id == repository_id,
        # A staged document is private until an import publishes a revision.
        published,
        # Metadata never reveals a source whose current selection no longer
        # authorizes the reader (for example a revoked fork dependency).
        Document.source_repository_id.in_(dependencies),
    )
    if position is not None:
        anchor, identity = position["path"], UUID(position["id"])
        if anchor is None:
            query = query.where(
                or_(
                    and_(Document.path.is_(None), Document.id > identity),
                    Document.path.is_not(None),
                )
            )
        else:
            query = query.where(
                or_(
                    Document.path > anchor,
                    and_(Document.path == anchor, Document.id > identity),
                )
            )
    rows = list(
        await db.scalars(
            query.order_by(Document.path.asc().nulls_first(), Document.id).limit(
                limit + 1
            )
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    summaries = await _published_summaries(db, [row.id for row in rows])
    items = []
    for row in rows:
        count, latest = summaries.get(row.id, (0, None))
        items.append(
            DocumentSummary(
                document_id=row.id,
                kind=row.kind,
                path=row.path,
                filename=_filename(row),
                deleted=row.deleted,
                published_revisions=count,
                latest_published_revision_id=(
                    latest.revision_id if latest is not None else None
                ),
                latest_published_at=(
                    latest.published_at if latest is not None else None
                ),
            )
        )
    next_position = (
        {"path": rows[-1].path, "id": str(rows[-1].id)} if more and rows else None
    )
    return items, next_position, generation


def _corpus_state(imported):
    if imported is None:
        return None
    return CorpusState(
        import_id=imported.id,
        kind=imported.kind,
        status=imported.status,
        target_generation=imported.target_generation,
        included=imported.included,
        skipped=imported.skipped,
        unsupported=imported.unsupported,
        over_limit=imported.over_limit,
        unscanned=imported.unscanned,
        complete=imported.complete,
        scan_reason=imported.scan_reason,
        processed_chunks=imported.processed_chunks,
        created_at=imported.created_at,
        deadline=imported.deadline,
    )


async def overview(service, actor, repository_id, *, limit, cursor, codec):
    """Repository-bound corpus selection, canonical state and a document page."""
    async with service.factory() as db:
        corpus = await db.get(KnowledgeCorpus, repository_id)
        observed = _snapshot(corpus)
        dependencies = await _dependencies(
            db, repository_id, corpus.target_workspace_id if corpus else None
        )
    grant = await service.access.verify(actor, dependencies, fresh=True)
    purpose = f"knowledge.documents:{repository_id}:{observed[0]}:{limit}"
    position = codec.decode_cursor(cursor, actor.id, purpose, limit) if cursor else None
    if position is not None and set(position) != {"path", "id"}:
        raise AppError("invalid_cursor", 400, "Invalid cursor")
    async with transaction(service.factory) as db:
        await service.access.lock(db, grant)
        corpus = await db.get(KnowledgeCorpus, repository_id, with_for_update=True)
        if _snapshot(corpus) != observed:
            raise changed()
        generation = corpus.target_generation if corpus else 0
        imported = (
            await db.scalar(
                select(KnowledgeImport).where(
                    KnowledgeImport.repository_id == repository_id,
                    KnowledgeImport.target_generation == generation,
                )
            )
            if corpus is not None
            else None
        )
        items, next_position, generation = await _document_page(
            db,
            repository_id,
            dependencies,
            generation,
            limit=limit,
            position=position,
        )
        active = (
            await db.get(IndexVersion, corpus.active_index_version_id)
            if corpus is not None and corpus.active_index_version_id is not None
            else None
        )
        target = (
            await db.scalar(
                select(IndexVersion)
                .where(
                    IndexVersion.repository_id == repository_id,
                    IndexVersion.target_generation == corpus.target_generation,
                )
                .order_by(IndexVersion.created_at.desc(), IndexVersion.id.desc())
                .limit(1)
            )
            if corpus is not None and corpus.target_generation > 0
            else None
        )
        availability = evaluate_index_availability(corpus, active, target)
        active_selectable = bool(
            active is not None
            and active.status == "ready"
            and active.queryable
            and active.purged_at is None
            and active.target_sha is not None
        )
        scopes = corpus.path_scopes if corpus else {}
        next_cursor = (
            codec.encode_cursor(actor.id, purpose, next_position, limit, max_limit=100)
            if next_position is not None
            else None
        )
        view = KnowledgeView(
            repository_id=repository_id,
            target_generation=generation,
            target_workspace_id=corpus.target_workspace_id if corpus else None,
            target_sha=corpus.target_sha if corpus else None,
            path_rule_version=(
                corpus.path_rule_version if corpus else PATH_RULE_VERSION
            ),
            document_paths=tuple(scopes.get("document_paths", ())),
            code_paths=tuple(scopes.get("code_paths", ())),
            corpus=_corpus_state(imported),
            needs_reimport=bool(
                imported is not None and imported.composition_version == 0
            ),
            index_status=availability.status,
            index_reason=availability.reason,
            active_index_generation=(
                active.target_generation if active_selectable else None
            ),
            active_index_sha=active.target_sha if active_selectable else None,
            documents=DocumentPage(items=tuple(items), next_cursor=next_cursor),
            limits=LIMITS,
        )
    # A database pointer alone cannot prove the vectors survived a restore.
    # Probe outside the locked transaction and fail explicitly rather than
    # serving a ready projection contradicted by storage.
    if availability.ready and service.index is not None:
        verified = await IndexAvailabilityService(
            service.factory, index=service.index
        ).verify(availability.index_id)
        if not verified.ready:
            raise AppError(
                "index_not_ready",
                409,
                f"Knowledge index is not ready ({verified.reason})",
            )
    return view


async def _revision_page(db, document_id, *, limit=REVISION_PAGE, position=None):
    query = (
        select(DocumentRevision)
        .options(defer(DocumentRevision.body))
        .where(
            DocumentRevision.document_id == document_id,
            DocumentRevision.published_at.is_not(None),
        )
    )
    if position is not None:
        anchor, identity = position["published_at"], UUID(position["id"])
        query = query.where(
            or_(
                DocumentRevision.published_at < anchor,
                and_(
                    DocumentRevision.published_at == anchor,
                    DocumentRevision.id < identity,
                ),
            )
        )
    rows = list(
        await db.scalars(
            query.order_by(
                DocumentRevision.published_at.desc(), DocumentRevision.id.desc()
            ).limit(limit + 1)
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_position = (
        {
            "published_at": rows[-1].published_at.isoformat(),
            "id": str(rows[-1].id),
        }
        if more and rows
        else None
    )
    return rows, next_position


def _revision_position(value):
    if value is None:
        return None
    if set(value) != {"published_at", "id"} or type(value["published_at"]) is not str:
        raise invalid_cursor()
    try:
        anchor = datetime.fromisoformat(value["published_at"])
        UUID(value["id"])
    except (TypeError, ValueError):
        raise invalid_cursor() from None
    if anchor.tzinfo is None:
        raise invalid_cursor()
    return {"published_at": anchor, "id": value["id"]}


async def _current_revisions(db, revision_ids):
    if not revision_ids:
        return set()
    rows = await db.scalars(
        select(ImportMember.revision_id)
        .join(KnowledgeImport, ImportMember.import_id == KnowledgeImport.id)
        .join(
            KnowledgeCorpus,
            KnowledgeImport.repository_id == KnowledgeCorpus.repository_id,
        )
        .where(
            ImportMember.revision_id.in_(revision_ids),
            ImportMember.status == "included",
            KnowledgeImport.status.in_(("ready", "ready_partial")),
            KnowledgeImport.target_generation == KnowledgeCorpus.target_generation,
        )
    )
    return set(rows)


async def document_view(
    service,
    actor,
    repository_id,
    document_id,
    *,
    revision_id,
    revision_limit,
    revision_cursor,
    start_line,
    line_count,
    cursor,
    codec,
):
    """Published revision metadata plus one explicitly selected bounded preview."""
    purpose = f"knowledge.revisions:{repository_id}:{document_id}:{revision_limit}"
    position = (
        _revision_position(
            codec.decode_cursor(revision_cursor, actor.id, purpose, revision_limit)
        )
        if revision_cursor
        else None
    )
    async with service.factory() as db:
        document = await db.get(Document, document_id)
        if document is None or document.repository_id != repository_id:
            raise not_found()
        corpus = await db.get(KnowledgeCorpus, repository_id)
        dependencies = await _dependencies(
            db, repository_id, corpus.target_workspace_id if corpus else None
        )
        if document.source_repository_id not in dependencies:
            raise not_found()
        revisions, next_position = await _revision_page(
            db, document_id, limit=revision_limit, position=position
        )
        if revision_id is None:
            selected = revisions[0] if revisions else None
        else:
            # An explicit revision may be older than this page; pagination only
            # bounds the list, never whether known published history is readable.
            selected = await db.scalar(
                select(DocumentRevision)
                .options(defer(DocumentRevision.body))
                .where(
                    DocumentRevision.id == revision_id,
                    DocumentRevision.document_id == document_id,
                    DocumentRevision.published_at.is_not(None),
                )
            )
            if selected is None:
                # Staged or unpublished revisions are absent from metadata and reads.
                raise not_found()
        selected_dependencies = (
            set(
                await db.scalars(
                    select(RevisionDependency.repository_id).where(
                        RevisionDependency.revision_id == selected.id
                    )
                )
            )
            if selected is not None
            else set()
        )
    grant = await service.access.verify(
        actor, dependencies | selected_dependencies, fresh=False
    )
    next_cursor = (
        codec.encode_cursor(
            actor.id, purpose, next_position, revision_limit, max_limit=100
        )
        if next_position is not None
        else None
    )
    async with transaction(service.factory) as db:
        await service.access.lock(db, grant)
        current = await db.get(Document, document_id, with_for_update=True)
        if current is None or current.deleted != document.deleted:
            raise changed()
        document = current
        is_current = await _current_revisions(db, [row.id for row in revisions])
        summaries = tuple(
            RevisionSummary(
                revision_id=row.id,
                revision_hash=row.revision_hash,
                published_at=row.published_at,
                is_current=row.id in is_current,
                availability=row.availability,
            )
            for row in revisions
        )
        view = DocumentView(
            document_id=document.id,
            repository_id=document.repository_id,
            kind=document.kind,
            path=document.path,
            filename=_filename(document),
            deleted=document.deleted,
            revisions=summaries,
            revisions_next_cursor=next_cursor,
            preview=None,
        )
    if selected is None or document.deleted:
        return view
    identity = SourceIdentity(
        repository_id=document.repository_id,
        source_repository_id=document.source_repository_id,
        dependency_repository_ids=tuple(sorted(selected_dependencies, key=str)),
        document_id=document.id,
        revision_hash=selected.revision_hash,
        workspace_id=selected.workspace_id,
        sha=selected.sha,
        path=document.path,
    )
    if service.reader is None:
        raise AppError("service_unavailable", 503, "Source reader unavailable")
    preview = await service.reader.preview(
        actor,
        identity,
        start_line=start_line,
        line_count=line_count,
        cursor=cursor,
    )
    return view.model_copy(update={"preview": preview})


async def paths_page(
    service,
    actor,
    repository_id,
    *,
    workspace_id,
    sha,
    document_paths,
    code_paths,
    limit,
    cursor,
    codec,
):
    """One bounded fixed-SHA metadata page with real policy outcomes."""
    if service.reader is None or service.reader.workspace is None:
        raise AppError("service_unavailable", 503, "Workspace reader unavailable")
    async with service.factory() as db:
        version = await db.get(WorkspaceVersion, workspace_id)
        if (
            version is None
            or version.repository_id != repository_id
            or version.status != "ready"
            or version.commit_sha != sha
        ):
            raise not_found()
        observed = (
            version.commit_sha,
            version.source_repository_id,
            tuple(sorted(scope["repository_id"] for scope in version.scopes)),
        )
        dependencies = await _dependencies(db, repository_id, workspace_id)
    grant = await service.access.verify(actor, dependencies, fresh=True)
    purpose = (
        "knowledge.paths:"
        f"{repository_id}:{workspace_id}:{sha}:{PATH_RULE_VERSION}:"
        f"{_scope_digest(document_paths, code_paths)}:{limit}"
    )
    position = codec.decode_cursor(cursor, actor.id, purpose, limit) if cursor else None
    if position is not None and (
        set(position) != {"ordinal"} or type(position["ordinal"]) is not int
    ):
        raise AppError("invalid_cursor", 400, "Invalid cursor")
    page = await service.reader.workspace.metadata(
        actor,
        repository_id,
        workspace_id,
        sha=sha,
        cursor=position["ordinal"] if position else 0,
        limit=limit,
    )
    deleted = set()
    paths = [entry.path for entry in page.items if entry.path is not None]
    if paths:
        async with service.factory() as db:
            deleted = set(
                await db.scalars(
                    select(Document.path).where(
                        Document.repository_id == repository_id,
                        Document.source_repository_id == version.source_repository_id,
                        Document.kind == "workspace",
                        Document.path.in_(paths),
                        Document.deleted.is_(True),
                    )
                )
            )
    items = []
    for entry in page.items:
        reason = (
            None
            if entry.path is None
            else path_reason(
                entry.path,
                document_paths=document_paths,
                code_paths=code_paths,
            )
        )
        if entry.path is None:
            outcome, reason = "excluded", "invalid_path"
        elif entry.path in deleted:
            outcome, reason = "excluded", "source_deleted"
        elif reason is None:
            outcome = "selected"
        elif reason == "unsupported":
            outcome = "unsupported"
        else:
            outcome = "excluded"
        items.append(
            PathEntry(
                path=entry.path,
                mode=entry.mode,
                size=entry.size,
                outcome=outcome,
                reason=reason,
            )
        )
    async with transaction(service.factory) as db:
        await service.access.lock(db, grant)
        current = await db.get(WorkspaceVersion, workspace_id, with_for_update=True)
        if (
            current is None
            or (
                current.commit_sha,
                current.source_repository_id,
                tuple(sorted(scope["repository_id"] for scope in current.scopes)),
            )
            != observed
            or current.status != "ready"
        ):
            raise changed()
    next_cursor = (
        codec.encode_cursor(
            actor.id,
            purpose,
            {"ordinal": page.next_cursor},
            limit,
            max_limit=100,
        )
        if page.next_cursor is not None
        else None
    )
    return PathPage(
        items=tuple(items),
        next_cursor=next_cursor,
        exhausted=page.exhausted,
        truncated=page.truncated,
    )


def _storage_exhausted():
    return AppError("storage_exhausted", 409, "Knowledge query storage exhausted")


def _invalid():
    return AppError("validation_error", 422, "Invalid knowledge query")


def _query_conflict():
    return AppError("idempotency_conflict", 409, "Idempotency key conflicts with input")


class QueryService:
    """Creator-private knowledge questions executed by the model worker.

    Submission freezes a durable selection and a Job. Execution happens in the
    worker: retrieval is re-authorized fresh, citations are derived from
    immutable evidence identity, and publication locks the full Lease, the
    active scope and the retained bytes in one transaction. A query payload is
    bounded and reclaimable; its Job visibility is not.
    """

    def __init__(self, factory, policy, *, retrieval, citations=None):
        self.factory, self.policy = factory, policy
        self.retrieval, self.citations = retrieval, citations
        self.access = KnowledgeAccess(policy)
        self.max_attempts = 3

    async def submit(self, actor: Actor, selection, question, key) -> QueryReceipt:
        try:
            chosen = QuerySelection.model_validate(selection)
            key = _KEY.validate_python(key)
        except (ValidationError, TypeError, ValueError):
            raise _invalid() from None
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question.encode("utf-8")) > QUESTION_LIMIT
        ):
            raise _invalid()
        repositories = chosen.selected_repository_ids
        anchor = repositories[0]
        chosen_data = chosen.model_dump(mode="json")
        input_hash = hashlib.sha256(
            json.dumps(
                {"question": question, "selection": chosen_data},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        try:
            grant = await self.access.verify(actor, repositories, fresh=True)
            async with transaction(self.factory) as db:
                await self.access.lock(db, grant)
                now = await db.scalar(select(func.clock_timestamp()))
                query_id = uuid4()
                job = await db.scalar(
                    insert(Job)
                    .values(
                        id=uuid4(),
                        kind=_QUERY_KIND,
                        repository_id=anchor,
                        requested_by=actor.id,
                        input={
                            "version": 1,
                            "query_id": str(query_id),
                            "selection": chosen_data,
                        },
                        input_hash=input_hash,
                        idempotency_key=key,
                        max_attempts=self.max_attempts,
                    )
                    .on_conflict_do_nothing(constraint="uq_jobs_creator_kind_key")
                    .returning(Job)
                )
                if job is None:
                    replay = await db.scalar(
                        select(Job).where(
                            Job.requested_by == actor.id,
                            Job.kind == _QUERY_KIND,
                            Job.idempotency_key == key,
                        )
                    )
                    if replay is None or replay.input_hash != input_hash:
                        raise _query_conflict()
                    existing = await db.scalar(
                        select(KnowledgeQuery.id).where(
                            KnowledgeQuery.job_id == replay.id
                        )
                    )
                    if existing is None:
                        raise not_found()
                    return QueryReceipt(query_id=existing, job_id=replay.id)
                active = await db.scalar(
                    select(func.count())
                    .select_from(KnowledgeQuery)
                    .where(
                        KnowledgeQuery.actor_id == actor.id,
                        KnowledgeQuery.reclaimed_at.is_(None),
                    )
                )
                if active >= QUERY_LIMIT:
                    oldest = await db.scalar(
                        select(KnowledgeQuery)
                        .join(Job, Job.id == KnowledgeQuery.job_id)
                        .where(
                            KnowledgeQuery.actor_id == actor.id,
                            KnowledgeQuery.reclaimed_at.is_(None),
                            Job.status.in_(_TERMINAL),
                        )
                        .order_by(KnowledgeQuery.created_at, KnowledgeQuery.id)
                        .limit(1)
                        .with_for_update(of=KnowledgeQuery, skip_locked=True)
                    )
                    if oldest is None:
                        raise AppError(
                            "budget_exhausted",
                            409,
                            "Knowledge query budget exhausted",
                        )
                    oldest.reclaimed_at = now
                    oldest.manifest = None
                    oldest.result = None
                    await db.execute(
                        delete(RevisionRetention).where(
                            RevisionRetention.owner_kind == "query",
                            RevisionRetention.owner_id == oldest.id,
                        )
                    )
                db.add(
                    KnowledgeQuery(
                        id=query_id,
                        job_id=job.id,
                        actor_id=actor.id,
                        question=question,
                        question_bytes=len(question.encode("utf-8")),
                        selection=chosen_data,
                        created_at=now,
                        retention_expires_at=now + RETENTION,
                    )
                )
                await db.flush()
                for ordinal, repository_id in enumerate(repositories):
                    db.add(
                        KnowledgeQueryRepository(
                            query_id=query_id,
                            repository_id=repository_id,
                            ordinal=ordinal,
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
                return QueryReceipt(query_id=query_id, job_id=job.id)
        except AppError as error:
            if error.code == "idempotency_conflict":
                async with self.factory.begin() as db:
                    await write_audit(
                        db,
                        AuditEntry(
                            actor_id=actor.id,
                            action="job.submitted",
                            resource_type="job",
                            outcome="failure",
                            reason_code="idempotency_conflict",
                        ),
                    )
            else:
                await self.policy.audit_denial(actor, error, anchor)
            raise

    async def run(self, lease) -> None:
        async with self.factory() as db:
            query = await db.scalar(
                select(KnowledgeQuery).where(KnowledgeQuery.job_id == lease.job_id)
            )
            if query is None or query.reclaimed_at is not None:
                raise not_found()
            user = await db.get(User, query.actor_id)
            if user is None or not user.enabled:
                raise not_found()
            actor = Actor(
                id=user.id,
                github_id=user.github_id,
                role=user.role,
                auth_version=user.auth_version,
            )
            try:
                selection = QuerySelection.model_validate(query.selection)
            except (ValidationError, TypeError, ValueError):
                raise AppError("internal_error", 500, "Invalid stored query") from None
            question = query.question
        scope = await self.retrieval.freeze(actor, selection, fresh=True)
        evidence = await self.retrieval.retrieve(
            RetrievalContext(
                lease=lease,
                scope=scope,
                deadline=datetime.now(UTC) + RETRIEVAL_DEADLINE,
            ),
            question,
        )
        if self.citations is None:
            raise AppError("service_unavailable", 503, "Citation service unavailable")
        manifest = await self.citations.manifest(evidence)
        try:
            metadata_bytes(manifest.model_dump(mode="json"), MANIFEST_LIMIT)
        except ValueError:
            raise _storage_exhausted() from None
        result = QueryResult(
            status=evidence.status,
            evidence=tuple(
                EvidenceView(
                    citation_id=entry.citation_id, text=item.text, rank=item.rank
                )
                for item, entry in zip(evidence.evidence, manifest.entries, strict=True)
            ),
            coverage=evidence.coverage,
        )
        try:
            metadata_bytes(result.model_dump(mode="json"), RESULT_LIMIT)
        except ValueError:
            raise _storage_exhausted() from None
        await self.retrieval.revalidate(evidence.scope, fresh=True)
        pairs = sorted(
            {
                (item.identity.document_id, item.identity.revision_hash)
                for item in evidence.evidence
            },
            key=lambda pair: (str(pair[0]), pair[1]),
        )
        async with transaction(self.factory) as db:
            # Same lock order as submission and ingestion: user, repositories,
            # then the Job lease row.
            await self.access.lock(db, evidence.scope.grant)
            job, now = await Queue(self.factory)._owned(db, lease)
            if job is None:
                raise AppError("lease_expired", 409, "Query lease expired")
            row = await db.get(
                KnowledgeQuery,
                query.id,
                with_for_update=True,
                populate_existing=True,
            )
            if row is None or row.reclaimed_at is not None or row.actor_id != actor.id:
                raise not_found()
            revision_ids: set[UUID] = set()
            if pairs:
                resolved = (
                    await db.execute(
                        select(
                            DocumentRevision.id,
                            DocumentRevision.document_id,
                            DocumentRevision.revision_hash,
                        ).where(
                            tuple_(
                                DocumentRevision.document_id,
                                DocumentRevision.revision_hash,
                            ).in_(pairs)
                        )
                    )
                ).all()
                by_pair = {
                    (item.document_id, item.revision_hash): item.id for item in resolved
                }
                if set(by_pair) != set(pairs):
                    raise AppError("internal_error", 500, "Evidence source unavailable")
                revision_ids = set(by_pair.values())
            await retain_sources(
                db,
                "query",
                row.id,
                tuple(sorted(revision_ids, key=str)),
                row.retention_expires_at,
            )
            row.manifest = manifest.model_dump(mode="json")
            row.result = result.model_dump(mode="json")
            row.completed_at = now
            job.status = "succeeded"
            job.error_code = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            await Queue._transition(db, job, "job.succeeded")

    async def get(self, actor: Actor, query_id: UUID) -> QueryView:
        async with self.factory() as db:
            query = await db.scalar(
                select(KnowledgeQuery).where(KnowledgeQuery.id == query_id)
            )
        if (
            query is None
            or query.actor_id != actor.id
            or query.reclaimed_at is not None
        ):
            async with self.factory.begin() as db:
                await check_actor(db, actor)
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="permission.denied",
                        resource_type="job",
                        resource_id=query_id,
                        outcome="failure",
                        reason_code="resource_not_found",
                    ),
                )
            raise not_found()
        try:
            selection = QuerySelection.model_validate(query.selection)
        except (ValidationError, TypeError, ValueError):
            raise AppError("internal_error", 500, "Invalid stored query") from None
        grant = await self.access.verify(
            actor, selection.selected_repository_ids, fresh=False
        )
        try:
            async with transaction(self.factory) as db:
                await self.access.lock(db, grant)
                row = await db.get(
                    KnowledgeQuery,
                    query_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    row is None
                    or row.reclaimed_at is not None
                    or row.actor_id != actor.id
                ):
                    raise not_found()
                job = await db.get(Job, row.job_id)
                if job is None:
                    raise not_found()
                return QueryView(
                    query_id=row.id,
                    job_id=row.job_id,
                    status=job.status,
                    selection=selection,
                    question=row.question,
                    manifest=EvidenceManifest.model_validate(row.manifest)
                    if row.manifest is not None
                    else None,
                    result=QueryResult.model_validate(row.result)
                    if row.result is not None
                    else None,
                    error_code=job.error_code,
                    created_at=row.created_at,
                    completed_at=row.completed_at,
                )
        except AppError as error:
            await self.policy.audit_denial(
                actor, error, selection.selected_repository_ids[0]
            )
            raise
