"""Retrieval pipeline: bounded search, DB recheck, RRF, rerank and evidence.

Real PostgreSQL and policy run here; the embedder, reranker and Milvus are
deterministic fakes. This proves the local contract, never model quality or
Milvus/model acceptance.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from support.index_env import SHA, make_index, ready_import, set_active
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import installation, repository

from devflow.errors import AppError
from devflow.jobs.queue import Lease
from devflow.knowledge.milvus import IndexFilter, RankedCandidates, RankedHit
from devflow.knowledge.models import Chunk, DocumentRevision, RevisionDependency
from devflow.knowledge.retrieval import RetrievalContext, RetrievalService
from devflow.knowledge.schemas import IndexSelection, QuerySelection

pytestmark = pytest.mark.asyncio


class FakeInference:
    """Deterministic token counts, vectors and scores; no model claim."""

    def __init__(self):
        self.embed_calls: list[tuple[str, ...]] = []
        self.rerank_calls: list[tuple[str, tuple[str, ...]]] = []
        self.token_calls: list[str] = []
        self.embedding_tokens: dict[str, int] = {}
        self.default_embedding_tokens = 1
        self.pair_tokens: dict[str, int] = {}
        self.default_pair_tokens = 10
        self.scores: dict[str, float] = {}
        self.fail_embed: BaseException | None = None
        self.fail_rerank: BaseException | None = None
        self.before_rerank = None

    async def count_embedding_tokens(self, text: str) -> int:
        self.token_calls.append(text)
        return self.embedding_tokens.get(text, self.default_embedding_tokens)

    async def embed(self, texts):
        self.embed_calls.append(tuple(texts))
        if self.fail_embed is not None:
            raise self.fail_embed
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def count_rerank_tokens(self, query: str, passage: str) -> int:
        return self.pair_tokens.get(passage, self.default_pair_tokens)

    async def rerank(self, query: str, passages):
        self.rerank_calls.append((query, tuple(passages)))
        if self.before_rerank is not None:
            await self.before_rerank(len(self.rerank_calls))
        if self.fail_rerank is not None:
            raise self.fail_rerank
        return [self.scores.get(passage, 0.0) for passage in passages]


class FakeIndex:
    """Synchronous Milvus-shaped fake; one result list per frozen index."""

    def __init__(self):
        self.results: dict[UUID, tuple[list[UUID], list[UUID]]] = {}
        self.calls: list[tuple[str, tuple[IndexFilter, ...], int]] = []
        self.fail: BaseException | None = None

    def search(self, query_text, query_vector, filters, *, limit=30):
        self.calls.append((query_text, tuple(filters), limit))
        if self.fail is not None:
            raise self.fail
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


def service(env, inference, index):
    return RetrievalService(env.factory, env.policy, inference=inference, index=index)


async def publish(env, *, generation=None, sha=SHA, chunk_count=2):
    fixture = await ready_import(
        env, generation=generation, sha=sha, chunk_count=chunk_count
    )
    row = await make_index(env, fixture, status="ready", chunk_ids=fixture.chunk_ids)
    await set_active(env, row.id)
    return fixture, row


async def frozen(env):
    return await RetrievalService(env.factory, env.policy).freeze(
        env.actor,
        QuerySelection(
            selected_repository_ids=(env.repository.id,),
            indexes=(
                IndexSelection(
                    repository_id=env.repository.id, sha=SHA, target="target"
                ),
            ),
        ),
    )


def context(scope, *, deadline=None, reserve=None):
    return RetrievalContext(
        lease=Lease(job_id=uuid4(), owner=uuid4(), generation=1, attempt=1),
        scope=scope,
        deadline=deadline or datetime.now(UTC) + timedelta(seconds=20),
        reserve_attempt=reserve,
    )


async def chunk_texts(env, chunk_ids) -> dict[UUID, str]:
    async with env.factory() as db:
        rows = (
            await db.execute(
                select(Chunk.id, Chunk.text).where(Chunk.id.in_(chunk_ids))
            )
        ).all()
    return {row.id: row.text for row in rows}


async def register_fork(env, number=202):
    env.installations = {1: ([installation(), installation(12)], None)}
    env.repositories[(12, 1)] = ([repository(number, "other/fork")], None)
    view = await env.service.register(env.actor, 12, number)
    return view.id


async def test_identical_filters_and_one_refill(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    texts = await chunk_texts(env, fixture.chunk_ids)
    valid = list(fixture.chunk_ids)
    missing = [uuid4() for _ in range(58)]
    inference = FakeInference()
    index = FakeIndex()
    # Both routes return a full 30 without the refill; only two hits exist in
    # PostgreSQL. Every other candidate has no written member and must be
    # dropped by the DB join before any passage reaches rerank.
    index.results[row.id] = (
        [valid[0], *missing],
        [valid[1], *reversed(missing)],
    )
    scope = await frozen(env)

    result = await service(env, inference, index).retrieve(context(scope), "question")

    assert len(index.calls) == 2
    assert [call[2] for call in index.calls] == [30, 60]
    filters = (IndexFilter(repository_id=env.repository.id, index_version_id=row.id),)
    assert index.calls[0][1] == filters
    assert index.calls[1][1] == filters
    assert index.calls[0][0] == index.calls[1][0] == "question"
    # Unauthorized candidates never produced canonical text or a rerank input;
    # this service has no source-body reader at all, only the DB chunk text.
    reranked = set(inference.rerank_calls[0][1])
    assert reranked == {texts[valid[0]], texts[valid[1]]}
    assert inference.embed_calls == [("question",)]
    assert len(result.evidence) == 1  # the fixture's two chunks overlap
    evidence = result.evidence[0]
    assert evidence.rank == 1
    assert evidence.chunk_id in valid
    assert evidence.index_version_id == row.id
    assert evidence.text == texts[evidence.chunk_id]
    assert evidence.content_hash == sha256(evidence.text.encode()).hexdigest()
    assert evidence.identity.repository_id == env.repository.id
    assert evidence.location.start_byte >= 0
    assert result.coverage[0].repository_id == env.repository.id
    assert result.coverage[0].target_generation == 1
    assert result.coverage[0].included == 1


async def test_one_version_change_retries_with_remaining_deadline(knowledge_env):
    env = knowledge_env
    first_fixture, first = await publish(env)
    inference = FakeInference()
    index = FakeIndex()
    index.results[first.id] = (list(first_fixture.chunk_ids), [])
    reserve_calls: list[bool] = []
    new_indexes: list[UUID] = []

    async def reserve(restarted: bool):
        reserve_calls.append(restarted)

    async def change_once(attempt):
        if attempt != 1:
            return
        fixture = await ready_import(env, generation=2, sha=SHA)
        row = await make_index(
            env, fixture, status="ready", chunk_ids=fixture.chunk_ids
        )
        await set_active(env, row.id)
        index.results[row.id] = (list(fixture.chunk_ids), [])
        new_indexes.append(row.id)

    inference.before_rerank = change_once
    scope = await frozen(env)
    result = await service(env, inference, index).retrieve(
        context(scope, reserve=reserve), "question"
    )

    assert reserve_calls == [False, True]
    assert len(index.calls) == 2
    assert index.calls[0][1][0].index_version_id == first.id
    assert index.calls[1][1][0].index_version_id == new_indexes[0]
    # The query was re-encoded and the DB rechecked against the new active
    # version; the returned scope is the new frozen scope.
    assert result.scope.indexes[0].index_version_id == new_indexes[0]
    assert result.evidence
    assert {item.index_version_id for item in result.evidence} == {new_indexes[0]}
    assert result.coverage[0].target_generation == 2


async def test_version_changed_once_then_stop(knowledge_env):
    env = knowledge_env
    first_fixture, first = await publish(env)
    inference = FakeInference()
    index = FakeIndex()
    index.results[first.id] = (list(first_fixture.chunk_ids), [])
    reserve_calls: list[bool] = []
    changed: list[UUID] = []

    async def reserve(restarted: bool):
        reserve_calls.append(restarted)

    async def change_on_every_rerank(attempt):
        fixture = await ready_import(env, generation=attempt + 1, sha=SHA)
        row = await make_index(
            env, fixture, status="ready", chunk_ids=fixture.chunk_ids
        )
        await set_active(env, row.id)
        index.results[row.id] = (list(fixture.chunk_ids), [])
        changed.append(row.id)

    inference.before_rerank = change_on_every_rerank
    scope = await frozen(env)
    with pytest.raises(AppError) as error:
        await service(env, inference, index).retrieve(
            context(scope, reserve=reserve), "question"
        )

    assert error.value.code == "version_changed"
    assert reserve_calls == [False, True]
    assert len(index.calls) == 2  # exactly one whole-retrieval retry
    assert len(changed) == 2
    assert index.calls[1][1][0].index_version_id == changed[0]


async def test_question_and_pair_limits(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    texts = await chunk_texts(env, fixture.chunk_ids)
    first, second = fixture.chunk_ids
    inference = FakeInference()
    index = FakeIndex()
    index.results[row.id] = ([first, second], [first, second])
    pipeline = service(env, inference, index)
    scope = await frozen(env)

    # 2048 UTF-8 bytes and 256 embedding-tokenizer tokens are both allowed.
    question = "a" * 2048
    inference.default_embedding_tokens = 256
    accepted = await pipeline.retrieve(context(scope), question)
    assert accepted.status == "evidence"
    assert inference.embed_calls[-1] == (question,)
    assert inference.rerank_calls[-1][0] == question

    # 2049 bytes is rejected before the tokenizer and never truncated.
    before = len(inference.token_calls)
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(context(scope), "b" * 2049)
    assert error.value.code == "validation_error"
    assert len(inference.token_calls) == before

    # 257 tokens is also rejected, not silently cut to 256.
    inference.embedding_tokens["q257"] = 257
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(context(scope), "q257")
    assert error.value.code == "validation_error"
    inference.embedding_tokens["q256"] = 256
    assert (await pipeline.retrieve(context(scope), "q256")).status == "evidence"

    for empty in ("", "   "):
        with pytest.raises(AppError) as error:
            await pipeline.retrieve(context(scope), empty)
        assert error.value.code == "validation_error"

    # Rerank query+passage including special tokens: 1024 is allowed, 1025 is
    # explicitly rejected rather than truncated and re-located.
    inference.embedding_tokens = {}
    inference.pair_tokens = {texts[first]: 1024, texts[second]: 1025}
    result = await pipeline.retrieve(context(scope), "pair")
    assert inference.rerank_calls[-1][1] == (texts[first],)
    assert [item.chunk_id for item in result.evidence] == [first]

    # Every unusable pair means no evidence, not a model failure.
    inference.pair_tokens[texts[first]] = 1025
    result = await pipeline.retrieve(context(scope), "pair")
    assert result.status == "no_evidence"
    assert result.evidence == ()


async def test_timeout_is_not_no_evidence(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    inference = FakeInference()
    index = FakeIndex()
    pipeline = service(env, inference, index)
    scope = await frozen(env)

    index.results[row.id] = ([], [])
    empty = await pipeline.retrieve(context(scope), "empty")
    assert empty.status == "no_evidence"
    assert empty.evidence == ()
    assert empty.coverage

    calls = len(index.calls)
    expired = context(scope, deadline=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(expired, "expired")
    assert error.value.code == "retrieval_unavailable"
    assert len(index.calls) == calls

    inference.fail_embed = AppError("service_unavailable", 503, "model down")
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(context(scope), "model")
    assert error.value.code == "retrieval_unavailable"
    inference.fail_embed = None

    index.fail = TimeoutError()
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(context(scope), "milvus")
    assert error.value.code == "retrieval_unavailable"
    index.fail = None

    # The empty search above was a real empty set, so it is safe to make the
    # same frozen index return candidates again for the model failure case.
    index.results[row.id] = (list(fixture.chunk_ids), [])
    inference.fail_rerank = TimeoutError()
    with pytest.raises(AppError) as error:
        await pipeline.retrieve(context(scope), "rerank")
    assert error.value.code == "retrieval_unavailable"


async def test_db_recheck_rejects_stale_and_unselected_sources(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    first, second = fixture.chunk_ids
    inference = FakeInference()
    index = FakeIndex()
    index.results[row.id] = ([first, second], [])
    pipeline = service(env, inference, index)
    scope = await frozen(env)

    initial = await pipeline.retrieve(context(scope), "question")
    assert initial.status == "evidence"

    # A revision that stopped being searchable is rechecked in DB, even though
    # the frozen index version itself is still current.
    async with env.factory.begin() as db:
        revision = await db.get(
            DocumentRevision, fixture.revision_ids[0], with_for_update=True
        )
        revision.searchable = False
    reranks = len(inference.rerank_calls)
    stale = await pipeline.retrieve(context(scope), "question")
    assert stale.status == "no_evidence"
    assert len(inference.rerank_calls) == reranks

    async with env.factory.begin() as db:
        revision = await db.get(
            DocumentRevision, fixture.revision_ids[0], with_for_update=True
        )
        revision.searchable = True

    # The actual dependency set must be a subset of the explicit selection.
    # Registering the fork changes policy epochs, so take a new frozen scope
    # before injecting the dependency row.
    fork_id = await register_fork(env)
    scope = await frozen(env)
    async with env.factory.begin() as db:
        db.add(
            RevisionDependency(
                revision_id=fixture.revision_ids[0], repository_id=fork_id
            )
        )
    unselected = await pipeline.retrieve(context(scope), "question")
    assert unselected.status == "no_evidence"
    assert len(inference.rerank_calls) == reranks
