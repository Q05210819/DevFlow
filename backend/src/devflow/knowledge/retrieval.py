"""Retrieve bounded, authorized and version-checked evidence.

The service freezes an explicitly selected index scope in PostgreSQL, embeds
the question locally, searches both routes with identical server-side filters,
re-checks every candidate against the database before reading canonical chunk
text, fuses with RRF, reranks locally and publishes a small, deduplicated
evidence set. It never truncates an over-limit question or a rerank pair, and a
model or vector-store failure is reported as unavailable, never as an honest
empty result.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, tuple_

from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.queue import Lease
from devflow.knowledge.access import KnowledgeAccess
from devflow.knowledge.indexing import evaluate_index_availability
from devflow.knowledge.milvus import IndexFilter, RankedCandidates, RankedHit
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    IndexMember,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
    RevisionDependency,
)
from devflow.knowledge.schemas import (
    ChunkLocation,
    CoverageView,
    Evidence,
    EvidenceSet,
    FrozenIndex,
    IndexSelection,
    QueryScope,
    QuerySelection,
    SourceGrant,
    SourceIdentity,
)

RRF_K = 60
INITIAL_LIMIT = 30
REFILL_LIMIT = 60
RERANK_LIMIT = 20
EVIDENCE_LIMIT = 6
PER_FILE_LIMIT = 2
MAX_QUESTION_BYTES = 2048
MAX_QUESTION_TOKENS = 256
MAX_RERANK_TOKENS = 1024
MILVUS_TIMEOUT_SECONDS = 5.0


def _changed():
    return AppError("version_changed", 409, "Knowledge target changed")


def _invalid():
    return AppError("validation_error", 422, "Invalid knowledge query scope")


def _invalid_question():
    return AppError("validation_error", 422, "Invalid knowledge question")


def _unavailable(message="Retrieval is unavailable"):
    return AppError("retrieval_unavailable", 503, message)


def _not_ready(reason):
    return AppError("index_not_ready", 409, f"Knowledge index is not ready ({reason})")


def _selection(value) -> QuerySelection:
    """Reject any shape that could smuggle a filter, epoch or index UUID."""
    try:
        return QuerySelection.model_validate(value)
    except ValidationError:
        raise _invalid() from None


def _expected_key(actor_id, epoch):
    # RepositoryPolicy.local's tuple is deliberately confined to this adapter.
    return (
        actor_id,
        epoch.repository_id,
        epoch.user_version,
        epoch.installation_version,
        epoch.repository_version,
        epoch.installation_id,
    )


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """One (index version, chunk) identity with both route ranks retained."""

    index_version_id: UUID
    chunk_id: UUID
    bm25_rank: int | None
    dense_rank: int | None
    score: float


def fuse(
    bm25: Sequence[RankedHit],
    dense: Sequence[RankedHit],
    *,
    k: int = RRF_K,
) -> tuple[FusedCandidate, ...]:
    """Reciprocal-rank fusion, deduplicated by index version and chunk ID."""

    if k < 1:
        raise ValueError("RRF k must be positive")
    bm25_ranks: dict[tuple[UUID, UUID], int] = {}
    dense_ranks: dict[tuple[UUID, UUID], int] = {}
    for hits, ranks in ((bm25, bm25_ranks), (dense, dense_ranks)):
        for hit in hits:
            key = (hit.index_version_id, hit.chunk_id)
            previous = ranks.get(key)
            if previous is None or hit.rank < previous:
                ranks[key] = hit.rank
    fused = []
    for key in set(bm25_ranks) | set(dense_ranks):
        bm25_rank = bm25_ranks.get(key)
        dense_rank = dense_ranks.get(key)
        score = sum(
            1.0 / (k + rank) for rank in (bm25_rank, dense_rank) if rank is not None
        )
        fused.append(
            FusedCandidate(
                index_version_id=key[0],
                chunk_id=key[1],
                bm25_rank=bm25_rank,
                dense_rank=dense_rank,
                score=score,
            )
        )
    fused.sort(
        key=lambda item: (-item.score, str(item.chunk_id), str(item.index_version_id))
    )
    return tuple(fused)


@dataclass(frozen=True, slots=True)
class RankedChunk:
    """An authorized chunk with canonical DB text and a rerank score."""

    chunk_id: UUID
    index_version_id: UUID
    identity: SourceIdentity
    location: ChunkLocation
    text: str
    content_hash: str
    score: float
    document_id: UUID
    revision_hash: str


def select_evidence(
    candidates: Sequence[RankedChunk],
    *,
    limit: int = EVIDENCE_LIMIT,
    per_file: int = PER_FILE_LIMIT,
) -> tuple[RankedChunk, ...]:
    """Apply the final caps without letting overlap or duplicates through."""

    selected: list[RankedChunk] = []
    seen_chunks: set[UUID] = set()
    per_file_count: dict[tuple[UUID, str], int] = {}
    intervals: dict[tuple[UUID, str], list[tuple[int, int]]] = {}
    for candidate in candidates:
        if candidate.chunk_id in seen_chunks:
            continue
        key = (candidate.document_id, candidate.revision_hash)
        if per_file_count.get(key, 0) >= per_file:
            continue
        start = candidate.location.start_byte
        end = candidate.location.end_byte
        if any(
            start < other_end and other_start < end
            for other_start, other_end in intervals.get(key, ())
        ):
            continue
        seen_chunks.add(candidate.chunk_id)
        per_file_count[key] = per_file_count.get(key, 0) + 1
        intervals.setdefault(key, []).append((start, end))
        selected.append(candidate)
        if len(selected) == limit:
            break
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Server-created only; never accepted as HTTP input."""

    lease: Lease
    scope: QueryScope
    deadline: datetime
    refresh_used: bool = False
    reserve_attempt: Callable[[bool], Awaitable[None]] | None = None


class RetrievalService:
    def __init__(self, factory, policy, *, access=None, inference=None, index=None):
        self.factory = factory
        self.policy = policy
        self.access = access or KnowledgeAccess(policy)
        self.inference = inference
        self.index = index

    async def freeze(
        self, actor, selection: QuerySelection, *, fresh: bool = True
    ) -> QueryScope:
        selection = _selection(selection)
        grant = await self.access.verify(
            actor, selection.selected_repository_ids, fresh=fresh
        )
        async with self.factory.begin() as db:
            await self.access.lock(db, grant)
            indexes = tuple(
                [
                    await self._freeze_index(db, item)
                    for item in sorted(
                        selection.indexes, key=lambda item: str(item.repository_id)
                    )
                ]
            )
        await self._unchanged(grant)
        return QueryScope(
            actor=actor,
            grant=grant,
            selected_repository_ids=tuple(selection.selected_repository_ids),
            indexes=indexes,
            selection=selection,
        )

    async def revalidate(self, scope: QueryScope, *, fresh: bool = False) -> None:
        grant = await self.access.verify(
            scope.actor, scope.selected_repository_ids, fresh=fresh
        )
        if grant.repository_epochs != scope.grant.repository_epochs:
            raise not_found()
        async with self.factory.begin() as db:
            await self.access.lock(db, grant)
            for frozen in scope.indexes:
                await self._revalidate_index(db, frozen)
        await self._unchanged(grant)

    async def retrieve(self, context: RetrievalContext, question: str) -> EvidenceSet:
        if self.inference is None or self.index is None:
            raise _unavailable("Retrieval is not configured")
        await self._question_tokens(context, question)
        try:
            return await self._bounded(context, question)
        except AppError as error:
            if error.code != "version_changed" or context.refresh_used:
                raise
            first = error
        if context.scope.selection is None:
            raise first
        scope = await self.freeze(
            context.scope.actor, context.scope.selection, fresh=False
        )
        retry = replace(context, scope=scope, refresh_used=True)
        return await self._bounded(retry, question)

    async def _bounded(self, context: RetrievalContext, question: str) -> EvidenceSet:
        remaining = self._remaining(context)
        try:
            return await asyncio.wait_for(self._attempt(context, question), remaining)
        except TimeoutError:
            raise _unavailable("Retrieval timed out") from None

    async def _attempt(self, context: RetrievalContext, question: str) -> EvidenceSet:
        if context.reserve_attempt is not None:
            await context.reserve_attempt(context.refresh_used)
        self._deadline(context)
        vector = await self._embed(question)
        candidates, dropped = await self._search_authorized(
            context, question, vector, limit=INITIAL_LIMIT
        )
        if dropped and len(candidates) < RERANK_LIMIT:
            # One refill per retrieval; still the same reserved attempt.
            candidates, _ = await self._search_authorized(
                context, question, vector, limit=REFILL_LIMIT
            )
        ranked = await self._rerank(question, candidates[:RERANK_LIMIT])
        # The publisher revalidates authorization, source set and active
        # version after the last model call and before returning anything.
        await self.revalidate(context.scope)
        selected = select_evidence(ranked)
        evidence = tuple(
            Evidence(
                identity=item.identity,
                chunk_id=item.chunk_id,
                index_version_id=item.index_version_id,
                location=item.location,
                text=item.text,
                content_hash=item.content_hash,
                rank=rank,
            )
            for rank, item in enumerate(selected, start=1)
        )
        coverage = await self._coverage(context.scope)
        return EvidenceSet(
            scope=context.scope,
            status="evidence" if evidence else "no_evidence",
            evidence=evidence,
            coverage=coverage,
        )

    async def _question_tokens(self, context: RetrievalContext, question: str) -> None:
        if not isinstance(question, str) or not question.strip():
            raise _invalid_question()
        if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
            raise _invalid_question()
        self._deadline(context)
        try:
            count = await asyncio.wait_for(
                self.inference.count_embedding_tokens(question),
                self._remaining(context),
            )
        except TimeoutError:
            raise _unavailable("Retrieval timed out") from None
        except Exception as error:
            raise _unavailable("Local inference failed") from error
        if not isinstance(count, int) or isinstance(count, bool):
            raise _unavailable("Local inference failed")
        if not 1 <= count <= MAX_QUESTION_TOKENS:
            raise _invalid_question()

    async def _embed(self, question: str) -> list[float]:
        try:
            vectors = await self.inference.embed([question])
        except Exception as error:
            raise _unavailable("Local inference failed") from error
        if not isinstance(vectors, (list, tuple)) or len(vectors) != 1:
            raise _unavailable("Local inference failed")
        vector = vectors[0]
        if not isinstance(vector, (list, tuple)) or not vector:
            raise _unavailable("Local inference failed")
        values = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _unavailable("Local inference failed")
            number = float(value)
            if not math.isfinite(number):
                raise _unavailable("Local inference failed")
            values.append(number)
        return values

    async def _search_authorized(
        self,
        context: RetrievalContext,
        question: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> tuple[tuple[RankedChunk, ...], int]:
        filters = tuple(
            IndexFilter(
                repository_id=item.repository_id,
                index_version_id=item.index_version_id,
            )
            for item in sorted(
                context.scope.indexes, key=lambda item: str(item.repository_id)
            )
        )
        timeout = min(MILVUS_TIMEOUT_SECONDS, self._remaining(context))
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.index.search,
                    question,
                    vector,
                    filters,
                    limit=limit,
                ),
                timeout,
            )
        except TimeoutError:
            raise _unavailable("Knowledge index search timed out") from None
        except Exception as error:
            raise _unavailable("Knowledge index search failed") from error
        if not isinstance(result, RankedCandidates):
            raise _unavailable("Knowledge index search failed")
        # Authorization happens before the rerank cap so dropped candidates
        # cannot occupy the 20 usable slots (the refill decision needs this).
        return await self._authorized(context.scope, fuse(result.bm25, result.dense))

    async def _authorized(
        self, scope: QueryScope, candidates: Sequence[FusedCandidate]
    ) -> tuple[tuple[RankedChunk, ...], int]:
        if not candidates:
            return (), 0
        pairs = [(item.index_version_id, item.chunk_id) for item in candidates]
        owners = {item.index_version_id: item.repository_id for item in scope.indexes}
        async with self.factory.begin() as db:
            for frozen in scope.indexes:
                await self._revalidate_index(db, frozen)
            rows = (
                (
                    await db.execute(
                        select(
                            IndexMember.index_version_id,
                            IndexMember.chunk_id,
                            Chunk.text,
                            Chunk.content_hash,
                            Chunk.start_line,
                            Chunk.end_line,
                            Chunk.start_byte,
                            Chunk.end_byte,
                            DocumentRevision.id,
                            DocumentRevision.document_id,
                            DocumentRevision.revision_hash,
                            DocumentRevision.workspace_id,
                            DocumentRevision.sha,
                            Document.repository_id,
                            Document.source_repository_id,
                            Document.path,
                        )
                        .join(Chunk, Chunk.id == IndexMember.chunk_id)
                        .join(
                            DocumentRevision, DocumentRevision.id == Chunk.revision_id
                        )
                        .join(Document, Document.id == DocumentRevision.document_id)
                        .where(
                            tuple_(
                                IndexMember.index_version_id, IndexMember.chunk_id
                            ).in_(pairs),
                            IndexMember.status == "written",
                            DocumentRevision.searchable.is_(True),
                            Document.deleted.is_(False),
                        )
                    )
                )
                .mappings()
                .all()
            )
            revision_ids = {row["id"] for row in rows}
            dependencies: dict[UUID, set[UUID]] = {}
            if revision_ids:
                dependency_rows = (
                    await db.execute(
                        select(
                            RevisionDependency.revision_id,
                            RevisionDependency.repository_id,
                        ).where(RevisionDependency.revision_id.in_(revision_ids))
                    )
                ).all()
                for revision_id, repository_id in dependency_rows:
                    dependencies.setdefault(revision_id, set()).add(repository_id)
        selected = set(scope.selected_repository_ids)
        by_identity = {(row["index_version_id"], row["chunk_id"]): row for row in rows}
        authorized = []
        for candidate in candidates:
            row = by_identity.get((candidate.index_version_id, candidate.chunk_id))
            if row is None:
                continue
            if owners.get(candidate.index_version_id) != row["repository_id"]:
                continue
            actual = dependencies.get(row["id"], set())
            if not {row["repository_id"], row["source_repository_id"]} <= actual:
                continue
            if not actual <= selected:
                continue
            try:
                identity = SourceIdentity(
                    repository_id=row["repository_id"],
                    source_repository_id=row["source_repository_id"],
                    dependency_repository_ids=tuple(sorted(actual, key=str)),
                    document_id=row["document_id"],
                    revision_hash=row["revision_hash"],
                    workspace_id=row["workspace_id"],
                    sha=row["sha"],
                    path=row["path"],
                )
                location = ChunkLocation(
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    start_byte=row["start_byte"],
                    end_byte=row["end_byte"],
                )
            except ValidationError:
                continue
            authorized.append(
                RankedChunk(
                    chunk_id=candidate.chunk_id,
                    index_version_id=candidate.index_version_id,
                    identity=identity,
                    location=location,
                    text=row["text"],
                    content_hash=row["content_hash"],
                    score=candidate.score,
                    document_id=row["document_id"],
                    revision_hash=row["revision_hash"],
                )
            )
        return tuple(authorized), len(candidates) - len(authorized)

    async def _rerank(
        self, question: str, candidates: Sequence[RankedChunk]
    ) -> tuple[RankedChunk, ...]:
        usable = []
        for candidate in candidates:
            try:
                count = await self.inference.count_rerank_tokens(
                    question, candidate.text
                )
            except Exception as error:
                raise _unavailable("Local inference failed") from error
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise _unavailable("Local inference failed")
            if count <= MAX_RERANK_TOKENS:
                usable.append(candidate)
        if not usable:
            return ()
        try:
            scores = await self.inference.rerank(
                question, [item.text for item in usable]
            )
        except Exception as error:
            raise _unavailable("Local inference failed") from error
        if not isinstance(scores, (list, tuple)) or len(scores) != len(usable):
            raise _unavailable("Local inference failed")
        ranked = []
        for candidate, score in zip(usable, scores):
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise _unavailable("Local inference failed")
            value = float(score)
            if not math.isfinite(value):
                raise _unavailable("Local inference failed")
            ranked.append((candidate, value))
        ranked.sort(
            key=lambda item: (
                -item[1],
                str(item[0].chunk_id),
                str(item[0].index_version_id),
            )
        )
        return tuple(replace(candidate, score=score) for candidate, score in ranked)

    async def _coverage(self, scope: QueryScope) -> tuple[CoverageView, ...]:
        views = []
        async with self.factory() as db:
            for frozen in sorted(
                scope.indexes, key=lambda item: str(item.repository_id)
            ):
                row = await db.scalar(
                    select(KnowledgeImport)
                    .where(
                        KnowledgeImport.repository_id == frozen.repository_id,
                        KnowledgeImport.target_generation == frozen.index_generation,
                    )
                    .limit(1)
                )
                views.append(
                    CoverageView(
                        repository_id=frozen.repository_id,
                        sha=frozen.sha,
                        target_generation=frozen.index_generation,
                        included=row.included if row is not None else 0,
                        skipped=row.skipped if row is not None else 0,
                        unsupported=row.unsupported if row is not None else 0,
                        over_limit=row.over_limit if row is not None else 0,
                        unscanned=row.unscanned if row is not None else True,
                        complete=row.complete if row is not None else False,
                    )
                )
        return tuple(views)

    def _remaining(self, context: RetrievalContext) -> float:
        remaining = (context.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise _unavailable("Retrieval timed out")
        return remaining

    def _deadline(self, context: RetrievalContext) -> None:
        self._remaining(context)

    async def _unchanged(self, grant: SourceGrant) -> None:
        for epoch in sorted(
            grant.repository_epochs, key=lambda item: str(item.repository_id)
        ):
            _, key = await self.policy.local(grant.actor, epoch.repository_id)
            if key != _expected_key(grant.actor.id, epoch):
                raise not_found()

    async def _freeze_index(self, db, item: IndexSelection) -> FrozenIndex:
        corpus = await db.get(
            KnowledgeCorpus,
            item.repository_id,
            with_for_update=True,
            populate_existing=True,
        )
        if corpus is None:
            raise _not_ready(
                "active_unavailable" if item.target == "active" else "no_target"
            )
        if item.target == "target":
            if corpus.target_generation <= 0 or not corpus.target_sha:
                raise _not_ready("no_target")
            if corpus.target_sha != item.sha:
                raise _changed()
            active = await self._active_row(db, corpus)
            target = await db.scalar(
                select(IndexVersion)
                .where(
                    IndexVersion.repository_id == item.repository_id,
                    IndexVersion.target_generation == corpus.target_generation,
                )
                .order_by(IndexVersion.created_at.desc(), IndexVersion.id.desc())
                .limit(1)
            )
            availability = evaluate_index_availability(corpus, active, target)
            availability.require()
            row = await db.get(IndexVersion, availability.index_id)
        else:
            row = await self._active_row(db, corpus)
            if row is None:
                raise _not_ready("active_unavailable")
            if row.status != "ready" or not row.queryable or row.purged_at is not None:
                raise _not_ready("active_unavailable")
            if row.target_sha != item.sha:
                raise _changed()
        if row is None or row.member_digest is None:
            raise _not_ready("vectors_missing")
        return FrozenIndex(
            repository_id=item.repository_id,
            index_version_id=row.id,
            sha=row.target_sha,
            index_generation=row.target_generation,
            target_generation=corpus.target_generation,
            member_digest=row.member_digest,
        )

    async def _revalidate_index(self, db, frozen: FrozenIndex) -> None:
        corpus = await db.get(
            KnowledgeCorpus,
            frozen.repository_id,
            with_for_update=True,
            populate_existing=True,
        )
        row = await db.get(
            IndexVersion,
            frozen.index_version_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            corpus is None
            or row is None
            or corpus.active_index_version_id != row.id
            or corpus.target_generation != frozen.target_generation
            or row.repository_id != frozen.repository_id
            or row.status != "ready"
            or not row.queryable
            or row.purged_at is not None
            or row.target_sha != frozen.sha
            or row.target_generation != frozen.index_generation
            or row.member_digest != frozen.member_digest
        ):
            raise _changed()

    @staticmethod
    async def _active_row(db, corpus: KnowledgeCorpus) -> IndexVersion | None:
        if corpus.active_index_version_id is None:
            return None
        return await db.get(
            IndexVersion,
            corpus.active_index_version_id,
            with_for_update=True,
            populate_existing=True,
        )
