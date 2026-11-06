"""Server-owned citation identities and bounded source previews.

Real PostgreSQL and policy; inference/Milvus are deterministic fakes. Citation
IDs and URLs are derived only from immutable evidence identity and position.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from support.index_env import SHA, make_index, ready_import, set_active
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

from devflow.errors import AppError
from devflow.jobs.queue import Lease
from devflow.knowledge.citations import CitationService, assign_ids
from devflow.knowledge.milvus import RankedCandidates, RankedHit
from devflow.knowledge.models import Chunk, Document, DocumentRevision, IndexMember
from devflow.knowledge.retrieval import RetrievalContext, RetrievalService
from devflow.knowledge.schemas import EvidenceSet, IndexSelection, QuerySelection
from devflow.knowledge.sources import SourceReader

pytestmark = pytest.mark.asyncio


class FakeInference:
    """Deterministic token counts, vectors and scores; no model claim."""

    async def count_embedding_tokens(self, text: str) -> int:
        return 1

    async def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def count_rerank_tokens(self, query: str, passage: str) -> int:
        return 10

    async def rerank(self, query: str, passages):
        return [0.0 for _ in passages]


class FakeIndex:
    """Synchronous Milvus-shaped fake; one result list per frozen index."""

    def __init__(self):
        self.results: dict[UUID, tuple[list[UUID], list[UUID]]] = {}

    def search(self, query_text, query_vector, filters, *, limit=30):
        bm25: list[RankedHit] = []
        dense: list[RankedHit] = []
        for item in filters:
            bucket = self.results.get(item.index_version_id)
            if bucket is None:
                continue
            for rank, chunk_id in enumerate(bucket[0][:limit], start=1):
                bm25.append(
                    RankedHit(
                        chunk_id=chunk_id,
                        index_version_id=item.index_version_id,
                        rank=rank,
                        score=0.0,
                    )
                )
            for rank, chunk_id in enumerate(bucket[1][:limit], start=1):
                dense.append(
                    RankedHit(
                        chunk_id=chunk_id,
                        index_version_id=item.index_version_id,
                        rank=rank,
                        score=0.0,
                    )
                )
        return RankedCandidates(bm25=tuple(bm25), dense=tuple(dense))


def selection(env):
    return QuerySelection(
        selected_repository_ids=(env.repository.id,),
        indexes=(
            IndexSelection(repository_id=env.repository.id, sha=SHA, target="target"),
        ),
    )


async def publish(env, *, chunk_count=2):
    fixture = await ready_import(env, chunk_count=chunk_count)
    row = await make_index(env, fixture, status="ready", chunk_ids=fixture.chunk_ids)
    await set_active(env, row.id)
    return fixture, row


def context(scope):
    return RetrievalContext(
        lease=Lease(job_id=uuid4(), owner=uuid4(), generation=1, attempt=1),
        scope=scope,
        deadline=datetime.now(UTC) + timedelta(seconds=20),
    )


async def retrieved(env, fixture, row):
    index = FakeIndex()
    index.results[row.id] = (list(fixture.chunk_ids), [])
    scope = await RetrievalService(env.factory, env.policy).freeze(
        env.actor, selection(env)
    )
    service = RetrievalService(
        env.factory, env.policy, inference=FakeInference(), index=index
    )
    return await service.retrieve(context(scope), "question")


def citations(env):
    return CitationService(
        env.factory,
        env.policy,
        public_base_url=env.settings.public_base_url,
        reader=SourceReader(env.factory, env.policy),
    )


def fragment(start_line, end_line):
    if start_line == end_line:
        return f"#L{start_line}"
    return f"#L{start_line}-L{end_line}"


async def separate_chunks(env, fixture):
    """Distinct byte intervals keep both fixture chunks as final evidence."""
    async with env.factory.begin() as db:
        chunks = list(
            await db.scalars(
                select(Chunk)
                .where(Chunk.id.in_(fixture.chunk_ids))
                .order_by(Chunk.ordinal)
                .with_for_update()
            )
        )
        chunks[0].start_byte, chunks[0].end_byte = 0, 10
        chunks[1].start_byte, chunks[1].end_byte = 20, 30


async def test_citation_identity_stable_and_unknown_rejected(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    await separate_chunks(env, fixture)
    evidence = await retrieved(env, fixture, row)
    assert [item.rank for item in evidence.evidence] == [1, 2]
    service = citations(env)

    first = await service.manifest(evidence)
    reordered = EvidenceSet(
        scope=evidence.scope,
        status=evidence.status,
        evidence=tuple(
            replace(item, rank=rank)
            for rank, item in enumerate(reversed(evidence.evidence), start=1)
        ),
        coverage=evidence.coverage,
    )
    second = await service.manifest(reordered)

    def mapping(manifest):
        return {
            (entry.identity, entry.location): entry.citation_id
            for entry in manifest.entries
        }

    assert mapping(first) == mapping(second)
    assert len(first.entries) == 2
    assert len(set(mapping(first).values())) == 2
    for entry in first.entries:
        assert re.fullmatch("[0-9a-f]{16}", entry.citation_id)
        assert entry.url == (
            f"https://github.com/team/private/blob/{SHA}/"
            f"{entry.identity.path}"
            f"{fragment(entry.location.start_line, entry.location.end_line)}"
        )
    assert {entry.rank for entry in first.entries} == {1, 2}

    # Short IDs are derived from immutable identity/location hashes; short
    # prefix collisions lengthen deterministically and an exact duplicate is
    # an internal invariant failure, never silently merged.
    assert assign_ids(["a" * 64, "b" * 64]) == ("a" * 16, "b" * 16)
    assert assign_ids(["f" * 63 + "0", "f" * 63 + "1"]) == (
        "f" * 63 + "0",
        "f" * 63 + "1",
    )
    with pytest.raises(AppError) as error:
        assign_ids(["c" * 64, "c" * 64])
    assert error.value.code == "internal_error"

    known = [entry.citation_id for entry in first.entries]
    service.validate_ids(first, known)
    with pytest.raises(AppError) as error:
        service.validate_ids(first, ["0" * 16])
    assert error.value.code == "answer_validation_failed"
    assert error.value.status == 422
    # A model-supplied URL is never accepted as a citation reference.
    with pytest.raises(AppError) as error:
        service.validate_ids(first, [first.entries[0].url])
    assert error.value.code == "answer_validation_failed"


async def test_old_citation_survives_vector_gc_but_not_source_revoke(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env, chunk_count=1)
    evidence = await retrieved(env, fixture, row)
    service = citations(env)
    manifest = await service.manifest(evidence)
    entry = manifest.entries[0]

    before = await service.preview(env.actor, manifest, entry.citation_id)
    assert before.reason is None
    assert before.text == "# indexed document\n"

    # Vector GC removes the index membership and the chunk row; the citation
    # still resolves from the retained canonical revision body.
    async with env.factory.begin() as db:
        await db.execute(
            delete(IndexMember).where(IndexMember.index_version_id == row.id)
        )
        await db.execute(delete(Chunk).where(Chunk.id.in_(fixture.chunk_ids)))

    after_gc = await service.preview(env.actor, manifest, entry.citation_id)
    assert after_gc.reason is None
    assert after_gc.text == before.text
    assert after_gc.identity == before.identity

    async with env.factory.begin() as db:
        document = await db.get(
            Document, entry.identity.document_id, with_for_update=True
        )
        document.deleted = True
    deleted = await service.preview(env.actor, manifest, entry.citation_id)
    assert deleted.reason == "source_unavailable"
    assert deleted.text is None
    assert deleted.next_cursor is None

    async with env.factory.begin() as db:
        document = await db.get(
            Document, entry.identity.document_id, with_for_update=True
        )
        document.deleted = False
        revision = await db.get(
            DocumentRevision, fixture.revision_ids[0], with_for_update=True
        )
        # A non-available revision may not stay searchable.
        revision.searchable = False
        revision.availability = "expired"
    expired = await service.preview(env.actor, manifest, entry.citation_id)
    assert expired.reason == "source_expired"
    assert expired.text is None

    # An unknown citation ID is a not-found resource, not a preview reason.
    with pytest.raises(AppError) as error:
        await service.preview(env.actor, manifest, "0" * 16)
    assert error.value.status == 404
