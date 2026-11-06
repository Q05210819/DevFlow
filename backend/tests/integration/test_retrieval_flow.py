"""Authorization boundaries across revocation, restart and version switch.

Real PostgreSQL, policy, queue, Milvus schema and Milvus searches are used. The
embedding and rerank fixtures are deterministic, so this proves the local
retrieval contract and its authorization fences, never model quality. Real
model inference, production hardware and private repositories remain NOT
EXECUTED.

The index is composed from a base repository and an explicitly selected fork
source. Every phase asserts that omitting the fork, deleting its source,
reusing a stale lease, restarting the client, or switching the active index
never exposes content outside the frozen selection.
"""

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from support.index_env import (
    CHUNKING,
    EMBEDDING,
    MODEL_TOKENIZER,
    NEXT_SHA,
    SHA,
    TOKENIZER,
    fingerprint,
)
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser
from support.sse import stream

from devflow.auth.models import Session, User
from devflow.auth.types import Actor
from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.models import GitHubCredential, GitHubInstallation
from devflow.jobs.events import JobEventService
from devflow.jobs.models import Job
from devflow.jobs.queue import Lease, Queue
from devflow.jobs.schemas import INTERACTIVE_MODEL_JOB_KINDS, MODEL_JOB_KINDS
from devflow.knowledge.citations import CitationService
from devflow.knowledge.indexing import IndexBuilder
from devflow.knowledge.inference import ModelFingerprint
from devflow.knowledge.milvus import DIMENSION, MilvusIndex
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
    KnowledgeQuery,
    RevisionDependency,
)
from devflow.knowledge.queries import QueryService
from devflow.knowledge.retrieval import RetrievalContext, RetrievalService
from devflow.knowledge.schemas import IndexSelection, QuerySelection
from devflow.knowledge.service import KnowledgeService
from devflow.knowledge.sources import workspace_source_version
from devflow.repositories.models import Repository

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("TEST_MILVUS_URI"),
        reason="TEST_MILVUS_URI is not set; real Milvus retrieval flow NOT EXECUTED",
    ),
]

MILVUS_URI = os.environ.get("TEST_MILVUS_URI")


class FlowInference:
    """One-hot deterministic embedder; it proves plumbing, never quality."""

    async def start(self) -> ModelFingerprint:
        return ModelFingerprint(
            embedding=EMBEDDING,
            reranker="flow-reranker-v1",
            tokenizer=MODEL_TOKENIZER,
            dimension=DIMENSION,
            device="cpu",
            dtype="float32",
        )

    async def embed(self, texts):
        return [self._vector(text) for text in texts]

    async def count_embedding_tokens(self, text: str) -> int:
        return 1

    async def count_rerank_tokens(self, query: str, passage: str) -> int:
        return 10

    async def rerank(self, query: str, passages):
        return [0.0 for _ in passages]

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [0.0] * DIMENSION
        seed = int.from_bytes(sha256(text.encode()).digest()[:8], "big")
        values[seed % DIMENSION] = 1.0
        return values


def make_index(collection: str) -> tuple[MilvusIndex, object]:
    from pymilvus import MilvusClient

    settings = Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        milvus_uri=MILVUS_URI,
        milvus_collection=collection,
        milvus_timeout_seconds=5,
        milvus_build_timeout_seconds=120,
    )
    client = MilvusClient(uri=MILVUS_URI, timeout=5)
    return MilvusIndex(settings, client=client), client


def close_index(client, collection: str) -> None:
    with suppress(Exception):
        client.drop_collection(collection)
    with suppress(Exception):
        client.close()


@dataclass(frozen=True)
class ComposedImport:
    import_id: UUID
    base_document_id: UUID
    fork_document_id: UUID


async def composed_import(
    env,
    fork_view,
    *,
    generation: int,
    sha: str,
    markers: tuple[str, str, str, str],
) -> ComposedImport:
    """One ready import holding a base document and a fork-sourced document."""
    now = datetime.now(UTC)
    import_id = uuid4()
    base_version = await env.workspace(sha=sha)
    fork_version = await env.workspace(source=fork_view, sha=sha)
    base_body = f"{markers[0]}\n{markers[1]}\n".encode()
    fork_body = f"{markers[2]}\n{markers[3]}\n".encode()
    async with env.factory.begin() as db:
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        if corpus is None:
            corpus = KnowledgeCorpus(repository_id=env.repository.id)
            db.add(corpus)
        corpus.target_generation = generation
        corpus.target_workspace_id = None
        corpus.target_sha = sha
        corpus.path_rule_version = "knowledge-paths-v1"
        corpus.path_scopes = {}
        user = await db.get(User, env.actor.id)
        repository = await db.get(Repository, env.repository.id)
        fork_repository = await db.get(Repository, fork_view.id)
        assert repository is not None and fork_repository is not None
        base_installation = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == repository.installation_id
            )
        )
        fork_installation = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == fork_repository.installation_id
            )
        )
        assert base_installation is not None and fork_installation is not None
        job = Job(
            id=uuid4(),
            kind="knowledge.import",
            repository_id=repository.id,
            requested_by=user.id,
            input={"import_id": str(import_id), "target_generation": generation},
            input_hash=sha256(f"{import_id}:{generation}".encode()).hexdigest(),
            idempotency_key=f"seed-{uuid4().hex}",
            status="succeeded",
        )
        db.add(job)
        await db.flush()
        db.add(
            KnowledgeImport(
                id=import_id,
                repository_id=repository.id,
                source_repository_id=repository.id,
                actor_id=user.id,
                actor_github_id=user.github_id,
                actor_auth_version=user.auth_version,
                kind="workspace",
                idempotency_key=f"import-{uuid4().hex}",
                request_hash=sha256(f"request-{import_id}".encode()).hexdigest(),
                request={},
                target_generation=generation,
                workspace_id=None,
                sha=sha,
                path_rule_version="knowledge-paths-v1",
                job_id=job.id,
                status="ready",
                created_at=now,
                deadline=now + timedelta(hours=24),
                cursor=2,
                included=2,
                skipped=0,
                unsupported=0,
                over_limit=0,
                scanned_bytes=len(base_body) + len(fork_body),
                failures=0,
                unscanned=False,
                complete=True,
                composition_version=1,
                target_workspace_id=None,
                target_sha=sha,
                target_path_scopes={},
                uploads_after=None,
                uploads_done=True,
                workspace_cursor=2,
                workspace_done=True,
                eligible_files=2,
                processed_chunks=4,
                tokenizer_fingerprint=TOKENIZER,
                chunking_version=CHUNKING,
            )
        )
        await db.flush()
        documents: dict[str, UUID] = {}
        entries = (
            (
                0,
                f"docs/g{generation}/base.md",
                base_version,
                repository.id,
                (repository.id,),
                base_body,
                (markers[0], markers[1]),
            ),
            (
                1,
                f"docs/g{generation}/fork.md",
                fork_version,
                fork_repository.id,
                (repository.id, fork_repository.id),
                fork_body,
                (markers[2], markers[3]),
            ),
        )
        for ordinal, path, version, source_id, dependencies, body, labels in entries:
            document = Document(
                id=uuid4(),
                repository_id=repository.id,
                source_repository_id=source_id,
                kind="workspace",
                path=path,
                attributes={},
            )
            revision = DocumentRevision(
                id=uuid4(),
                document_id=document.id,
                revision_hash=sha256(body).hexdigest(),
                source_version=workspace_source_version(
                    repository_id=repository.id,
                    source_repository_id=source_id,
                    dependency_repository_ids=dependencies,
                    workspace_id=version.id,
                    sha=version.commit_sha,
                    path=path,
                ),
                workspace_id=version.id,
                sha=version.commit_sha,
                body=body,
                body_bytes=len(body),
                searchable=True,
                availability="available",
                published_at=now,
            )
            db.add_all([document, revision])
            await db.flush()
            for dependency in dependencies:
                db.add(
                    RevisionDependency(
                        revision_id=revision.id,
                        repository_id=dependency,
                    )
                )
            db.add(
                ImportMember(
                    id=uuid4(),
                    import_id=import_id,
                    source_key=path,
                    ordinal=ordinal,
                    revision_id=revision.id,
                    status="included",
                    member_kind="workspace",
                    next_chunk=2,
                    total_chunks=2,
                    declared_bytes=len(body),
                )
            )
            first_end = len(labels[0].encode()) + 1
            for index, label in enumerate(labels):
                text = f"{label} context"
                db.add(
                    Chunk(
                        id=uuid4(),
                        revision_id=revision.id,
                        ordinal=index,
                        text=text,
                        embedding_text=f"{path}\n{text}",
                        content_hash=sha256(text.encode()).hexdigest(),
                        token_count=3,
                        tokenizer_fingerprint=TOKENIZER,
                        chunking_version=CHUNKING,
                        start_line=index + 1,
                        end_line=index + 1,
                        start_byte=0 if index == 0 else first_end,
                        end_byte=first_end if index == 0 else len(body),
                        attributes={},
                    )
                )
            documents[path] = document.id
        db.add(
            ImportRepositoryEpoch(
                import_id=import_id,
                repository_id=repository.id,
                installation_id=repository.installation_id,
                user_version=user.auth_version,
                installation_version=base_installation.auth_version,
                repository_version=repository.auth_version,
            )
        )
        db.add(
            ImportRepositoryEpoch(
                import_id=import_id,
                repository_id=fork_repository.id,
                installation_id=fork_repository.installation_id,
                user_version=user.auth_version,
                installation_version=fork_installation.auth_version,
                repository_version=fork_repository.auth_version,
            )
        )
        await db.flush()
    return ComposedImport(
        import_id=import_id,
        base_document_id=documents[f"docs/g{generation}/base.md"],
        fork_document_id=documents[f"docs/g{generation}/fork.md"],
    )


async def publish_import(env, index: MilvusIndex, composed: ComposedImport):
    build = IndexBuilder(
        KnowledgeService(env.factory, env.policy),
        inference=FlowInference(),
        index=index,
    )
    receipt = await build.request(env.actor, composed.import_id, f"flow-{uuid4().hex}")
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, lease_seconds=300).claim(
        uuid4(), allowed_kinds=MODEL_JOB_KINDS
    )
    assert lease is not None and lease.job_id == receipt.job_id
    await build.run_batch(lease)
    async with env.factory() as db:
        row = await db.get(IndexVersion, receipt.index_id)
    assert row is not None
    return receipt, row


async def claim_job(env, job_id):
    lease = await Queue(env.factory).claim(
        uuid4(),
        allowed_kinds=MODEL_JOB_KINDS,
        prefer_kinds=INTERACTIVE_MODEL_JOB_KINDS,
    )
    assert lease is not None and lease.job_id == job_id
    return lease


async def run_query(env, service, chosen, question, key):
    receipt = await service.submit(env.actor, chosen, question, key)
    await service.run(await claim_job(env, receipt.job_id))
    view = await service.get(env.actor, receipt.query_id)
    return receipt, view


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


def entry_at(manifest, *, path: str, line: int):
    return next(
        item
        for item in manifest.entries
        if item.identity.path == path and item.location.start_line == line
    )


async def test_retrieval_flow_revocation_and_version_switch(knowledge_env):
    env = knowledge_env
    fork_view = await env.fork()
    fork = fork_view
    v1 = await composed_import(
        env,
        fork_view,
        generation=1,
        sha=SHA,
        markers=("alphamarker", "betamarker", "forkmarker", "forksecondary"),
    )
    collection = f"flow_{uuid4().hex[:16]}"
    adapter, client = make_index(collection)
    restart_client = None
    try:
        _, v1_row = await publish_import(env, adapter, v1)
        citations = CitationService(
            env.factory, env.policy, public_base_url=env.settings.public_base_url
        )
        retrieval = RetrievalService(
            env.factory, env.policy, inference=FlowInference(), index=adapter
        )
        service = QueryService(
            env.factory, env.policy, retrieval=retrieval, citations=citations
        )
        base_only = QuerySelection(
            selected_repository_ids=(env.repository.id,),
            indexes=(
                IndexSelection(
                    repository_id=env.repository.id, sha=SHA, target="target"
                ),
            ),
        )
        both = QuerySelection(
            selected_repository_ids=(env.repository.id, fork.id),
            indexes=(
                IndexSelection(
                    repository_id=env.repository.id, sha=SHA, target="target"
                ),
            ),
        )

        # A: an index built from base and fork content is not widened when the
        # fork is registered but omitted from the user's selection.
        _, a_view = await run_query(env, service, base_only, "alphamarker", "flow-a")
        assert a_view.status == "succeeded"
        assert a_view.manifest is not None and a_view.result is not None
        assert a_view.result.status == "evidence"
        assert any("alphamarker" in item.text for item in a_view.result.evidence)
        assert all(
            entry.identity.source_repository_id == env.repository.id
            for entry in a_view.manifest.entries
        )
        assert all("forkmarker" not in item.text for item in a_view.result.evidence)

        # B: an explicit fork selection yields a fork citation whose preview is
        # pinned to the immutable source. Unknown IDs and other accounts fail.
        b_receipt, b_view = await run_query(env, service, both, "forkmarker", "flow-b")
        assert b_view.status == "succeeded"
        assert b_view.manifest is not None and b_view.result is not None
        fork_entries = [
            entry
            for entry in b_view.manifest.entries
            if entry.identity.source_repository_id == fork.id
        ]
        assert fork_entries
        b_entry = entry_at(b_view.manifest, path="docs/g1/fork.md", line=1)
        assert b_entry.url == (
            f"https://github.com/other/fork/blob/{SHA}/docs/g1/fork.md#L1"
        )
        preview = await citations.preview(
            env.actor, b_view.manifest, b_entry.citation_id
        )
        assert preview.text is not None and "forkmarker" in preview.text
        assert preview.location is not None
        assert preview.location.start_line == 1
        assert preview.is_current is True
        with pytest.raises(AppError) as error:
            await citations.preview(env.actor, b_view.manifest, "0" * 16)
        assert error.value.status == 404

        other = await other_user(env)
        with pytest.raises(AppError) as error:
            await env.jobs.get_visible(other, b_receipt.job_id)
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await service.get(other, b_receipt.query_id)
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await JobEventService(env.jobs).read_events(
                other, b_receipt.job_id, after=0
            )
        assert error.value.status == 404
        async with browser(env) as (_, app, raw):
            path = f"/api/v1/jobs/{b_receipt.job_id}/events?after=0"
            async with stream(app, path, raw) as response:
                assert response.status == 200
                received = [event_data(await response.body()) for _ in range(3)]
                assert received[-1][1]["status"] == "succeeded"
                await response.finished()
            async with env.factory.begin() as db:
                await db.execute(update(Session).values(user_id=other.id))
            async with stream(app, path, raw) as response:
                assert response.status == 404
                assert (
                    json.loads(await response.body())["error"]["code"]
                    == "resource_not_found"
                )
            async with env.factory.begin() as db:
                await db.execute(update(Session).values(user_id=env.actor.id))

        # C: deleting the fork source revokes its evidence everywhere, while
        # authorized base evidence may still be returned. The old citation
        # reports source_unavailable instead of leaking retained bytes.
        async with env.factory.begin() as db:
            document = await db.get(Document, v1.fork_document_id, with_for_update=True)
            assert document is not None
            document.deleted = True
        gone = await citations.preview(env.actor, b_view.manifest, b_entry.citation_id)
        assert gone.reason == "source_unavailable"
        assert gone.text is None and gone.location is None
        assert gone.is_current is False and gone.partial is False
        assert gone.next_cursor is None
        _, c_view = await run_query(env, service, both, "forkmarker", "flow-c")
        assert c_view.status == "succeeded"
        assert c_view.manifest is not None and c_view.result is not None
        assert c_view.result.status == "evidence"
        assert all(
            entry.identity.source_repository_id == env.repository.id
            for entry in c_view.manifest.entries
        )
        assert all("forkmarker" not in item.text for item in c_view.result.evidence)

        # D: a stale executor loses the lease after retrieval but before the
        # citation publication fence; recovery retries and succeeds.
        d_receipt = await service.submit(env.actor, base_only, "betamarker", "flow-d")
        first = await claim_job(env, d_receipt.job_id)
        async with env.factory.begin() as db:
            job = await db.get(Job, d_receipt.job_id, with_for_update=True)
            assert job is not None
            job.lease_generation += 1
        with pytest.raises(AppError) as error:
            await service.run(first)
        assert error.value.code == "lease_expired"
        assert error.value.status == 409
        async with env.factory() as db:
            job = await db.get(Job, d_receipt.job_id, populate_existing=True)
            query = await db.get(
                KnowledgeQuery, d_receipt.query_id, populate_existing=True
            )
            assert job is not None and query is not None
            assert job.status == "running"
            assert query.manifest is None and query.result is None
        async with env.factory.begin() as db:
            job = await db.get(Job, d_receipt.job_id, with_for_update=True)
            assert job is not None
            job.lease_expires_at = await db.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)
        await Queue(env.factory).recover_expired()
        async with env.factory() as db:
            job = await db.get(Job, d_receipt.job_id, populate_existing=True)
            assert job is not None and job.status == "retry_wait"
        await env.due(d_receipt.job_id)
        second = await claim_job(env, d_receipt.job_id)
        assert second.attempt == 2
        await service.run(second)
        d_view = await service.get(env.actor, d_receipt.query_id)
        assert d_view.status == "succeeded"
        assert d_view.result is not None and d_view.result.status == "evidence"

        # E: a fresh client revalidates the published schema and still serves
        # the same frozen selection after a process restart.
        restart_index, restart_client = make_index(collection)
        restart_index.ensure_schema(fingerprint())
        restarted = QueryService(
            env.factory,
            env.policy,
            retrieval=RetrievalService(
                env.factory,
                env.policy,
                inference=FlowInference(),
                index=restart_index,
            ),
            citations=CitationService(
                env.factory,
                env.policy,
                public_base_url=env.settings.public_base_url,
            ),
        )
        _, e_view = await run_query(env, restarted, base_only, "betamarker", "flow-e")
        assert e_view.status == "succeeded"
        assert e_view.result is not None
        assert e_view.result.status == "evidence"
        assert any("betamarker" in item.text for item in e_view.result.evidence)

        # F: publishing generation two retires the active index. An in-flight
        # scope and a new old-SHA query fail as version_changed; old citations
        # remain readable but are marked non-current.
        stale_scope = await retrieval.freeze(env.actor, base_only)
        v2 = await composed_import(
            env,
            fork_view,
            generation=2,
            sha=NEXT_SHA,
            markers=("gammamarker", "deltamarker", "epsilonmarker", "zetamarker"),
        )
        _, v2_row = await publish_import(env, adapter, v2)
        async with env.factory() as db:
            corpus = await db.get(KnowledgeCorpus, env.repository.id)
            old_row = await db.get(IndexVersion, v1_row.id)
            assert corpus is not None and old_row is not None
            assert corpus.active_index_version_id == v2_row.id
            assert corpus.target_generation == 2
            assert corpus.target_sha == NEXT_SHA
            assert old_row.status == "retired"
            assert old_row.queryable is False
        with pytest.raises(AppError) as error:
            await retrieval.retrieve(
                RetrievalContext(
                    lease=Lease(uuid4(), uuid4(), 1, 1),
                    scope=stale_scope,
                    deadline=datetime.now(UTC) + timedelta(seconds=20),
                ),
                "betamarker",
            )
        assert error.value.code == "version_changed"
        assert error.value.status == 409

        stale = QuerySelection(
            selected_repository_ids=(env.repository.id,),
            indexes=(
                IndexSelection(
                    repository_id=env.repository.id, sha=SHA, target="target"
                ),
            ),
        )
        old_receipt = await service.submit(env.actor, stale, "betamarker", "flow-f-old")
        old_lease = await claim_job(env, old_receipt.job_id)
        with pytest.raises(AppError) as error:
            await service.run(old_lease)
        assert error.value.code == "version_changed"
        assert error.value.status == 409
        await Queue(env.factory).fail(old_lease, "version_changed", False)
        async with env.factory() as db:
            old_query = await db.get(
                KnowledgeQuery, old_receipt.query_id, populate_existing=True
            )
            old_job = await db.get(Job, old_receipt.job_id, populate_existing=True)
            assert old_query is not None and old_job is not None
            assert old_query.manifest is None and old_query.result is None
            assert old_job.status == "failed"

        current = QuerySelection(
            selected_repository_ids=(env.repository.id, fork.id),
            indexes=(
                IndexSelection(
                    repository_id=env.repository.id,
                    sha=NEXT_SHA,
                    target="target",
                ),
            ),
        )
        _, f_view = await run_query(
            env, service, current, "epsilonmarker", "flow-f-new"
        )
        assert f_view.status == "succeeded"
        assert f_view.manifest is not None and f_view.result is not None
        f_entry = entry_at(f_view.manifest, path="docs/g2/fork.md", line=1)
        assert f_entry.identity.sha == NEXT_SHA
        assert f_entry.url == (
            f"https://github.com/other/fork/blob/{NEXT_SHA}/docs/g2/fork.md#L1"
        )
        f_preview = await citations.preview(
            env.actor, f_view.manifest, f_entry.citation_id
        )
        assert f_preview.text is not None
        assert "epsilonmarker" in f_preview.text
        assert f_preview.is_current is True

        a_entry = entry_at(a_view.manifest, path="docs/g1/base.md", line=1)
        stale_preview = await citations.preview(
            env.actor, a_view.manifest, a_entry.citation_id
        )
        assert stale_preview.text is not None
        assert "alphamarker" in stale_preview.text
        assert stale_preview.is_current is False
    finally:
        close_index(client, collection)
        if restart_client is not None:
            close_index(restart_client, collection)
