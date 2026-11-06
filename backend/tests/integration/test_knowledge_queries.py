"""Private query Jobs and bounded query storage.

Real PostgreSQL, policy, queue and retrieval pipeline; inference and Milvus are
deterministic fakes. This proves the local contract, never model or production
acceptance.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from support.index_env import SHA, make_index, ready_import, set_active
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser
from support.sse import stream

from devflow.auth.models import Session, User
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.models import GitHubCredential
from devflow.jobs.events import JobEventService
from devflow.jobs.models import Job
from devflow.jobs.queue import Lease, Queue
from devflow.jobs.schemas import INTERACTIVE_MODEL_JOB_KINDS, MODEL_JOB_KINDS
from devflow.knowledge.citations import CitationService
from devflow.knowledge.milvus import RankedCandidates, RankedHit
from devflow.knowledge.models import KnowledgeQuery, RevisionRetention
from devflow.knowledge.queries import QueryService
from devflow.knowledge.retrieval import RetrievalContext, RetrievalService
from devflow.knowledge.schemas import (
    EvidenceManifest,
    EvidenceSet,
    IndexSelection,
    QuerySelection,
)
from devflow.repositories.models import Repository

pytestmark = pytest.mark.asyncio


class FakeInference:
    """Deterministic token counts, vectors and scores; no model claim."""

    def __init__(self):
        self.embedding_tokens: dict[str, int] = {}
        self.default_embedding_tokens = 1
        self.pair_tokens: dict[str, int] = {}
        self.default_pair_tokens = 10
        self.scores: dict[str, float] = {}

    async def count_embedding_tokens(self, text: str) -> int:
        return self.embedding_tokens.get(text, self.default_embedding_tokens)

    async def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def count_rerank_tokens(self, query: str, passage: str) -> int:
        return self.pair_tokens.get(passage, self.default_pair_tokens)

    async def rerank(self, query: str, passages):
        return [self.scores.get(passage, 0.0) for passage in passages]


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


class FixedRetrieval:
    """Reuses a real frozen scope so publishing still locks real epochs."""

    def __init__(self, scope, evidence):
        self.scope = scope
        self.evidence = evidence
        self.freeze_calls = 0

    async def freeze(self, actor, selection, *, fresh=True):
        self.freeze_calls += 1
        return self.scope

    async def retrieve(self, context, question):
        return self.evidence

    async def revalidate(self, scope, *, fresh=False):
        return None


class FixedCitations:
    def __init__(self, manifest):
        self._manifest = manifest

    async def manifest(self, evidence):
        return self._manifest


def selection(env, *repository_ids):
    ids = repository_ids or (env.repository.id,)
    return QuerySelection(
        selected_repository_ids=ids,
        indexes=(
            IndexSelection(repository_id=env.repository.id, sha=SHA, target="target"),
        ),
    )


def retrieval(env, inference, index):
    return RetrievalService(env.factory, env.policy, inference=inference, index=index)


def query_service(env, inference, index, citations):
    return QueryService(
        env.factory,
        env.policy,
        retrieval=retrieval(env, inference, index),
        citations=citations,
    )


async def publish(env):
    fixture = await ready_import(env)
    row = await make_index(env, fixture, status="ready", chunk_ids=fixture.chunk_ids)
    await set_active(env, row.id)
    return fixture, row


async def claim_job(env, job_id):
    lease = await Queue(env.factory).claim(
        uuid4(),
        allowed_kinds=MODEL_JOB_KINDS,
        prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
    )
    assert lease is not None and lease.job_id == job_id
    return lease


async def run_job(env, service, job_id):
    await service.run(await claim_job(env, job_id))


async def other_user(env, github_id=43, role="member"):
    async with env.factory.begin() as db:
        user = User(github_id=github_id, role=role)
        db.add(user)
        await db.flush()
        db.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=env.cipher.encrypt(
                    SecretStr("other-user-token")
                ),
            )
        )
        actor = Actor(user.id, user.github_id, user.role, user.auth_version)
    await env.policy.require(actor, env.repository.id)
    return actor


def event_data(body):
    lines = body.strip().splitlines()
    assert lines[1] == "event: job.status"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert set(payload) == {"status", "attempt", "error_code"}
    return int(lines[0].removeprefix("id: ")), payload


async def test_query_private_job_and_sse(knowledge_env):
    env = knowledge_env
    fork_id = (await env.fork()).id
    fixture, row = await publish(env)
    inference = FakeInference()
    index = FakeIndex()
    index.results[row.id] = (list(fixture.chunk_ids), [])
    citations = CitationService(
        env.factory, env.policy, public_base_url=env.settings.public_base_url
    )
    service = query_service(env, inference, index, citations)
    chosen = selection(env, env.repository.id, fork_id)

    receipt = await service.submit(env.actor, chosen, "what is indexed?", "private-0")
    async with env.factory() as db:
        job = await db.get(Job, receipt.job_id)
        query = await db.get(KnowledgeQuery, receipt.query_id)
        assert job.kind == "knowledge.query"
        assert job.requested_by == env.actor.id
        assert job.result is None
        assert query.question == "what is indexed?"
        assert query.selection["selected_repository_ids"] == [
            str(env.repository.id),
            str(fork_id),
        ]
    visible = await env.jobs.get_visible(env.actor, receipt.job_id)
    assert visible.id == receipt.job_id

    # A second account never sees the creator-private Job or its query.
    other = await other_user(env)
    env.calls.clear()
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(other, receipt.job_id)
    assert error.value.status == 404
    assert env.calls == []
    with pytest.raises(AppError) as error:
        await service.get(other, receipt.query_id)
    assert error.value.status == 404
    with pytest.raises(AppError) as error:
        await service.get(env.actor, uuid4())
    assert error.value.status == 404

    await run_job(env, service, receipt.job_id)
    view = await service.get(env.actor, receipt.query_id)
    assert view.status == "succeeded"
    assert view.manifest is not None and view.manifest.entries
    assert view.result is not None and view.result.status == "evidence"
    assert view.result.evidence

    # Event replay is creator-private and honours the caller's cursor.
    replay = await JobEventService(env.jobs).read_events(
        env.actor, receipt.job_id, after=1
    )
    assert [event.sequence for event in replay] == [2, 3]
    assert replay[-1].status == "succeeded"

    async with browser(env) as (_, app, raw):
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 200
            received = [event_data(await response.body()) for _ in range(3)]
            assert [sequence for sequence, _ in received] == [1, 2, 3]
            assert received[-1][1]["status"] == "succeeded"
            await response.finished()

        # Another account cannot subscribe to the same Job.
        async with env.factory.begin() as db:
            await db.execute(update(Session).values(user_id=other.id))
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 404
            assert (
                json.loads(await response.body())["error"]["code"]
                == "resource_not_found"
            )
        async with env.factory.begin() as db:
            await db.execute(update(Session).values(user_id=env.actor.id))

        # Revoking any participating repository, even the one that owns no
        # index, revokes the whole query, its events and its citations.
        async with env.factory.begin() as db:
            await db.execute(
                update(Repository)
                .where(Repository.id == fork_id)
                .values(
                    available=False,
                    auth_version=Repository.auth_version + 1,
                )
            )
        with pytest.raises(AppError) as error:
            await env.jobs.get_visible(env.actor, receipt.job_id)
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await JobEventService(env.jobs).read_events(
                env.actor, receipt.job_id, after=0
            )
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await service.get(env.actor, receipt.query_id)
        assert error.value.status == 404
        async with stream(
            app, f"/api/v1/jobs/{receipt.job_id}/events?after=0", raw
        ) as response:
            assert response.status == 404
            assert (
                json.loads(await response.body())["error"]["code"]
                == "resource_not_found"
            )


async def test_query_storage_caps(knowledge_env):
    env = knowledge_env
    fixture, row = await publish(env)
    inference = FakeInference()
    index = FakeIndex()
    index.results[row.id] = (list(fixture.chunk_ids), [])
    citations = CitationService(
        env.factory, env.policy, public_base_url=env.settings.public_base_url
    )
    service = query_service(env, inference, index, citations)
    chosen = selection(env)

    # The immutable question/selection is kept, but never inside Job.result.
    receipt = await service.submit(env.actor, chosen, "storage", "cap-0")
    async with env.factory() as db:
        query = await db.get(KnowledgeQuery, receipt.query_id)
        job = await db.get(Job, receipt.job_id)
        created, expires = query.created_at, query.retention_expires_at
        assert expires - created == timedelta(hours=1)
        assert job.result is None

    await run_job(env, service, receipt.job_id)
    view = await service.get(env.actor, receipt.query_id)
    assert view.status == "succeeded"
    async with env.factory() as db:
        retained = set(
            await db.scalars(
                select(RevisionRetention.revision_id).where(
                    RevisionRetention.owner_kind == "query",
                    RevisionRetention.owner_id == receipt.query_id,
                )
            )
        )
    assert retained == set(fixture.revision_ids)

    receipts = [receipt]
    for number in range(1, 20):
        receipts.append(
            await service.submit(
                env.actor, chosen, f"question {number}", f"cap-{number}"
            )
        )

    # Eighteen older queries are complete and readable; the newest query is
    # still active and must not be reclaimed by someone else's submission.
    now = datetime.now(UTC)
    async with env.factory.begin() as db:
        for old in receipts[1:19]:
            job = await db.get(Job, old.job_id, with_for_update=True)
            job.status = "succeeded"
            query = await db.get(KnowledgeQuery, old.query_id, with_for_update=True)
            query.completed_at = now
            query.manifest = {"entries": []}
            query.result = {"status": "no_evidence", "evidence": [], "coverage": []}
        active = await db.get(
            KnowledgeQuery, receipts[19].query_id, with_for_update=True
        )
        active.manifest = {"entries": []}
        active.result = {"status": "no_evidence", "evidence": [], "coverage": []}
        for owner in (receipts[1].query_id, receipts[19].query_id):
            db.add(
                RevisionRetention(
                    owner_kind="query",
                    owner_id=owner,
                    revision_id=fixture.revision_ids[0],
                    expires_at=now + timedelta(hours=1),
                )
            )

    extra = await service.submit(env.actor, chosen, "question 20", "cap-20")
    async with env.factory() as db:
        oldest = await db.get(KnowledgeQuery, receipt.query_id, populate_existing=True)
        assert oldest.reclaimed_at is not None
        assert oldest.manifest is None and oldest.result is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RevisionRetention)
                .where(
                    RevisionRetention.owner_kind == "query",
                    RevisionRetention.owner_id == receipt.query_id,
                )
            )
            == 0
        )
        second = await db.get(
            KnowledgeQuery, receipts[1].query_id, populate_existing=True
        )
        assert second.reclaimed_at is None and second.manifest is not None
        active = await db.get(
            KnowledgeQuery, receipts[19].query_id, populate_existing=True
        )
        assert active.reclaimed_at is None and active.manifest is not None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RevisionRetention)
                .where(
                    RevisionRetention.owner_kind == "query",
                    RevisionRetention.owner_id == receipts[19].query_id,
                )
            )
            == 1
        )

    # With no terminal query left, the 20-query budget is exhausted.
    async with env.factory.begin() as db:
        for pending in [*receipts[1:], extra]:
            job = await db.get(Job, pending.job_id, with_for_update=True)
            job.status = "running"
    with pytest.raises(AppError) as error:
        await service.submit(env.actor, chosen, "over budget", "cap-21")
    assert error.value.code == "budget_exhausted"
    assert error.value.status == 409

    # Release every earlier Job so a different user's oversized attempts are
    # the only claimable work; budgets are per creator.
    async with env.factory.begin() as db:
        for pending in [*receipts[1:], extra]:
            job = await db.get(Job, pending.job_id, with_for_update=True)
            job.status = "failed"

    other = await other_user(env, github_id=77)
    real_scope = await RetrievalService(env.factory, env.policy).freeze(other, chosen)
    real_evidence = await retrieval(env, inference, index).retrieve(
        RetrievalContext(
            lease=Lease(job_id=uuid4(), owner=uuid4(), generation=1, attempt=1),
            scope=real_scope,
            deadline=datetime.now(UTC) + timedelta(seconds=20),
        ),
        "question",
    )
    base_entry = (await citations.manifest(real_evidence)).entries[0]

    # A manifest over 64 KiB is rejected before it can be persisted.
    huge_manifest = EvidenceManifest(
        entries=(replace(base_entry, url="https://example.com/" + "x" * 70000),)
    )
    manifest_service = QueryService(
        env.factory,
        env.policy,
        retrieval=FixedRetrieval(real_scope, real_evidence),
        citations=FixedCitations(huge_manifest),
    )
    oversized_manifest = await manifest_service.submit(
        other, chosen, "huge manifest", "oversize-manifest"
    )
    lease = await claim_job(env, oversized_manifest.job_id)
    with pytest.raises(AppError) as error:
        await manifest_service.run(lease)
    assert error.value.code == "storage_exhausted"
    await Queue(env.factory).fail(lease, "storage_exhausted", False)

    # A result over 128 KiB is likewise rejected after the manifest fits.
    huge_text = replace(real_evidence.evidence[0], text="x" * 140000)
    huge_evidence = EvidenceSet(
        scope=real_evidence.scope,
        status="evidence",
        evidence=(huge_text,),
        coverage=real_evidence.coverage,
    )
    small_manifest = EvidenceManifest(entries=(replace(base_entry, citation_id="c0"),))
    result_service = QueryService(
        env.factory,
        env.policy,
        retrieval=FixedRetrieval(real_scope, huge_evidence),
        citations=FixedCitations(small_manifest),
    )
    oversized_result = await result_service.submit(
        other, chosen, "huge result", "oversize-result"
    )
    lease = await claim_job(env, oversized_result.job_id)
    with pytest.raises(AppError) as error:
        await result_service.run(lease)
    assert error.value.code == "storage_exhausted"
