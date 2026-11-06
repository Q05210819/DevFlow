"""Real graph plumbing with a scripted model transport.

Everything below the provider transport is real: the Job lease, retrieval
pipeline, citation manifest, budget ledger, official LangGraph saver and the
terminal run/Job transition. Only ``ChatProvider.invoke`` is scripted, so these
tests prove bounded control flow and durable recovery, never real answer
quality or provider acceptance.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select

from devflow.chat.budget import BudgetService
from devflow.chat.graph import ChatGraph
from devflow.chat.handlers import ChatRunHandler
from devflow.chat.models import AgentRun, BudgetLedger, Message, RequestReceipt
from devflow.chat.models import ToolCall as RunToolCall
from devflow.chat.provider import ChatProvider, ProviderReply
from devflow.chat.schemas import AnswerDraft, ProviderUsage
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.knowledge.citations import CitationService
from devflow.knowledge.milvus import IndexFilter, RankedCandidates, RankedHit
from devflow.knowledge.retrieval import RetrievalService
from devflow.knowledge.schemas import IndexSelection, QuerySelection
from support.index_env import SHA, make_index, ready_import, set_active
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414

USD = "USD"
PRICING = {
    "chat_cloud_enabled": True,
    "chat_budget_currency": USD,
    "chat_run_budget": Decimal("1.000000"),
    "chat_month_budget": Decimal("2.000000"),
    "chat_budget_minor_unit": Decimal("0.000001"),
    "chat_pricing_version": "test-pricing-v1",
    "chat_pricing_valid_until": datetime.now(UTC) + timedelta(days=1),
    "chat_input_price_per_million": Decimal("0.500000"),
    "chat_output_price_per_million": Decimal("1.500000"),
}


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


class ScriptedProvider(ChatProvider):
    """Real request serialization plus a deterministic, one-shot reply script.

    ``prepare`` always goes through ``ChatProvider.prepare`` so the body bytes,
    hash and 24 KiB limit stay authoritative. ``invoke`` records the prepared
    call, awaits an optional hook and returns the scripted reply for that call
    index. ``prepare_fail_at`` and ``fail_invoke_at`` are zero-based call
    indexes that raise exactly once, proving resumable boundaries.
    """

    def __init__(self, script=()):
        super().__init__("scripted-test-key", None)
        self.script = tuple(script)
        self.prepare_calls: list = []
        self.invoked: list = []
        self.prepare_fail_at: int | None = None
        self.prepare_error: BaseException | None = None
        self.fail_invoke_at: int | None = None
        self.invoke_error: BaseException | None = None
        self.before_invoke = None

    def prepare(self, messages, tools, manifest):
        index = len(self.prepare_calls)
        self.prepare_calls.append(manifest)
        if self.prepare_fail_at == index:
            raise self.prepare_error or AppError(
                "service_unavailable", 503, "Scripted prepare failure"
            )
        return ChatProvider.prepare(self, messages, tools, manifest)

    async def invoke(self, prepared, *, deadline):
        index = len(self.invoked)
        self.invoked.append(prepared)
        if self.before_invoke is not None:
            await self.before_invoke(index)
        if self.fail_invoke_at == index:
            raise self.invoke_error or AppError(
                "service_unavailable", 503, "Scripted invoke failure"
            )
        mode = self.script[index] if index < len(self.script) else "answer"
        usage = ProviderUsage(
            prompt_tokens=1000,
            completion_tokens=500,
            request_id=f"req-{index}",
            complete=True,
        )
        if mode == "tool":
            return ProviderReply(
                answer=None,
                tool_query="more evidence",
                tool_call_id=f"call-{index}",
                usage=usage,
            )
        entries = prepared.manifest.entries
        draft = AnswerDraft(
            conclusion=f"answer {index}",
            citation_ids=(entries[0].citation_id,) if entries else (),
            insufficient_evidence=not entries,
            next_steps=(),
        )
        return ProviderReply(
            answer=draft, tool_query=None, tool_call_id=None, usage=usage
        )


def selection(env, sha=SHA) -> QuerySelection:
    return QuerySelection(
        selected_repository_ids=(env.repository.id,),
        indexes=(
            IndexSelection(repository_id=env.repository.id, sha=sha, target="target"),
        ),
    )


async def publish(env, index, *, generation=None, sha=SHA, chunk_count=2):
    fixture = await ready_import(
        env, generation=generation, sha=sha, chunk_count=chunk_count
    )
    row = await make_index(env, fixture, status="ready", chunk_ids=fixture.chunk_ids)
    await set_active(env, row.id)
    if fixture.chunk_ids:
        index.results[row.id] = (list(fixture.chunk_ids), list(fixture.chunk_ids))
    return fixture, row


async def create_run(env, label):
    view = await env.chat.create(env.actor, (env.repository.id,), f"{label}-create")
    return await env.chat.submit(
        env.actor,
        view.id,
        selection(env),
        f"question {label}",
        f"{label}-run",
    )


def retry_queue(env):
    """Zero backoff so a retried attempt is claimable immediately."""

    return Queue(
        env.factory,
        lease_seconds=300,
        retry_first_seconds=0,
        retry_second_seconds=0,
    )


async def claim(env):
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=frozenset({"chat.run"})
    )
    assert lease is not None
    return lease


async def fail(env, lease, code, *, retryable=True):
    assert await retry_queue(env).fail(lease, code, retryable)


def build(env, provider, inference=None, index=None):
    settings = env.settings.model_copy(update=PRICING)
    inference = inference or FakeInference()
    index = index or FakeIndex()
    retrieval = RetrievalService(
        env.factory,
        env.policy,
        access=env.chat.access,
        inference=inference,
        index=index,
    )
    citations = CitationService(
        env.factory, env.policy, public_base_url=env.settings.public_base_url
    )
    budget = BudgetService(env.factory, settings)
    graph = ChatGraph(
        env.factory,
        settings,
        retrieval=retrieval,
        citations=citations,
        budget=budget,
        provider=provider,
    )
    handler = ChatRunHandler(graph, env.factory, env.policy)
    return SimpleNamespace(
        graph=graph,
        handler=handler,
        retrieval=retrieval,
        citations=citations,
        budget=budget,
        provider=provider,
        inference=inference,
        index=index,
        settings=settings,
    )


async def snapshot(env, run_id):
    """Load every row a run may have produced, detached from the session."""

    async with env.factory() as db:
        run = await db.scalar(select(AgentRun).where(AgentRun.id == run_id))
        job = await db.scalar(select(Job).where(Job.id == run.job_id))
        calls = list(
            await db.scalars(
                select(RunToolCall)
                .where(RunToolCall.run_id == run_id)
                .order_by(RunToolCall.ordinal)
            )
        )
        messages = list(
            await db.scalars(
                select(Message)
                .where(Message.run_id == run_id)
                .order_by(Message.ordinal)
            )
        )
        receipts = list(
            await db.scalars(
                select(RequestReceipt)
                .where(RequestReceipt.run_id == run_id)
                .order_by(RequestReceipt.ordinal)
            )
        )
        ledgers = list(
            await db.scalars(select(BudgetLedger).where(BudgetLedger.run_id == run_id))
        )
    return SimpleNamespace(
        run=run,
        job=job,
        calls=calls,
        messages=messages,
        receipts=receipts,
        ledgers=ledgers,
    )
