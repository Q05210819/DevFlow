"""Fixed-set retrieval quality on the real Task 2 pipeline.

The gold set is frozen in ``quality.json`` as immutable source SHAs, paths and
inclusive line intervals; it never names chunks produced by the implementation.
Each run seeds the real workspace revisions and chunks with ``LocalChunker``,
publishes through ``IndexBuilder``/``MilvusIndex`` and measures BM25, dense,
RRF and RRF+rerank with the same gold and the same dedupe rules.

Real local models, a real Milvus instance and a real PostgreSQL are required.
Without ``TEST_MILVUS_URI`` and the ``TEST_MODEL_*`` variables this module is
reported NOT EXECUTED; a deterministic fake ranker is never substituted under
the quality label, and no production acceptance is claimed from a skip.
"""

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import installation, repository
from support.retrieval_quality import (
    RetrievalMatch,
    score_questions,
    validate_quality,
)

from devflow.auth.models import User
from devflow.config import Settings
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job
from devflow.jobs.queue import Lease, Queue
from devflow.jobs.schemas import MODEL_JOB_KINDS
from devflow.knowledge.chunking import LocalChunker
from devflow.knowledge.indexing import IndexBuilder
from devflow.knowledge.inference import LocalInference
from devflow.knowledge.milvus import IndexFilter, MilvusIndex
from devflow.knowledge.models import (
    Chunk,
    Document,
    DocumentRevision,
    ImportMember,
    ImportRepositoryEpoch,
    IndexVersion,
    KnowledgeCorpus,
    KnowledgeImport,
    RevisionDependency,
)
from devflow.knowledge.retrieval import (
    RetrievalContext,
    RetrievalService,
    fuse,
    select_evidence,
)
from devflow.knowledge.schemas import (
    IndexSelection,
    QuerySelection,
    SourceBytes,
    SourceIdentity,
)
from devflow.knowledge.service import KnowledgeService
from devflow.knowledge.sources import workspace_source_version
from devflow.repositories.models import Repository
from devflow.workspace.models import WorkspaceVersion
from devflow.workspace.scope import capture

RUNTIME_ENV = {
    "TEST_MILVUS_URI": os.environ.get("TEST_MILVUS_URI"),
    "TEST_MODEL_EMBEDDING_PATH": os.environ.get("TEST_MODEL_EMBEDDING_PATH"),
    "TEST_MODEL_RERANKER_PATH": os.environ.get("TEST_MODEL_RERANKER_PATH"),
    "TEST_MODEL_DEVICE": os.environ.get("TEST_MODEL_DEVICE"),
    "TEST_MODEL_DTYPE": os.environ.get("TEST_MODEL_DTYPE"),
    "TEST_MODEL_TOKENIZER_PATH": os.environ.get("TEST_MODEL_TOKENIZER_PATH"),
}
MISSING = [name for name, value in RUNTIME_ENV.items() if not value]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        bool(MISSING),
        reason=(
            "real model/Milvus quality run NOT EXECUTED; missing " + ", ".join(MISSING)
        ),
    ),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"
GOLD = FIXTURES / "quality.json"
K = 6
SEARCH_LIMIT = 30
METHODS = ("bm25", "dense", "rrf", "rrf_rerank")
TARGET = 0.85
BATCH_BUDGET = 8


def runtime_settings(root: Path) -> Settings:
    paths = {
        "TEST_MODEL_EMBEDDING_PATH": Path(RUNTIME_ENV["TEST_MODEL_EMBEDDING_PATH"]),
        "TEST_MODEL_RERANKER_PATH": Path(RUNTIME_ENV["TEST_MODEL_RERANKER_PATH"]),
        "TEST_MODEL_TOKENIZER_PATH": Path(RUNTIME_ENV["TEST_MODEL_TOKENIZER_PATH"]),
    }
    for name, path in paths.items():
        assert path.is_absolute(), f"{name} must be an absolute directory"
        assert path.is_dir(), f"{name} does not exist: {path}"
    timeout = os.environ.get("TEST_MODEL_STARTUP_TIMEOUT_SECONDS", "1800")
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url="http://localhost:3000",
        worker_role="model",
        model_embedding_path=str(paths["TEST_MODEL_EMBEDDING_PATH"]),
        model_reranker_path=str(paths["TEST_MODEL_RERANKER_PATH"]),
        model_device=RUNTIME_ENV["TEST_MODEL_DEVICE"],
        model_dtype=RUNTIME_ENV["TEST_MODEL_DTYPE"],
        model_lock_path=root / "quality-model.lock",
        model_startup_timeout_seconds=float(timeout),
        model_drain_timeout_seconds=120,
        knowledge_tokenizer_path=str(paths["TEST_MODEL_TOKENIZER_PATH"]),
        milvus_uri=RUNTIME_ENV["TEST_MILVUS_URI"],
        milvus_collection="quality_" + uuid4().hex[:16],
        milvus_timeout_seconds=5,
        milvus_build_timeout_seconds=120,
    )


def make_index(settings: Settings) -> tuple[MilvusIndex, object]:
    from pymilvus import MilvusClient

    client = MilvusClient(uri=settings.milvus_uri, timeout=5)
    return MilvusIndex(settings, client=client), client


def close_index(client, collection: str) -> None:
    with suppress(Exception):
        client.drop_collection(collection)
    with suppress(Exception):
        client.close()


async def register_repositories(env, gold):
    env.installations = {1: ([installation()], None)}
    env.repositories[(11, 1)] = (
        [
            repository(101),
            repository(301, "team/legacy"),
            repository(302, "team/isolated"),
        ],
        None,
    )
    views = {"base": env.repository}
    views["legacy"] = await env.service.register(env.actor, 11, 301)
    views["isolated"] = await env.service.register(env.actor, 11, 302)
    assert set(views) == set(gold["repositories"])
    return views


def draft_files(view, *, root: Path, sha: str, workspace_id):
    """Chunk every frozen file with the production tokenizer and chunker."""
    chunker = LocalChunker(Path(RUNTIME_ENV["TEST_MODEL_TOKENIZER_PATH"]))
    files = sorted(path for path in root.rglob("*") if path.is_file())
    assert files
    entries = []
    for path in files:
        body = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        document_id = uuid4()
        identity = SourceIdentity(
            repository_id=view.id,
            source_repository_id=view.id,
            dependency_repository_ids=(view.id,),
            document_id=document_id,
            revision_hash=sha256(body).hexdigest(),
            workspace_id=workspace_id,
            sha=sha,
            path=relative,
        )
        drafts = chunker.split(
            SourceBytes(identity=identity, body=body), title=relative
        )
        assert drafts, f"{relative} produced no chunks"
        entries.append((relative, body, document_id, identity, drafts))
    fingerprints = {
        draft.tokenizer_fingerprint for *_, drafts in entries for draft in drafts
    }
    versions = {draft.chunking_version for *_, drafts in entries for draft in drafts}
    assert len(fingerprints) == 1 and len(versions) == 1
    return entries, fingerprints.pop(), versions.pop()


async def seed_import(
    env, view, *, root: Path, sha: str, generation: int, cache_id: int
):
    """Create one ready import holding the frozen fixture files."""
    now = datetime.now(UTC)
    import_id = uuid4()
    workspace_id = uuid4()
    workspace_job_id = uuid4()
    import_job_id = uuid4()
    scopes = [await capture(env.policy, env.actor, view.id, fresh=True)]
    entries, fingerprint, chunking_version = draft_files(
        view, root=root, sha=sha, workspace_id=workspace_id
    )
    async with env.factory.begin() as db:
        user = await db.get(User, env.actor.id)
        repository_row = await db.get(Repository, view.id)
        assert user is not None and repository_row is not None
        installation_row = await db.scalar(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == repository_row.installation_id
            )
        )
        assert installation_row is not None
        db.add(
            Job(
                id=workspace_job_id,
                kind="repository.workspace",
                repository_id=view.id,
                requested_by=user.id,
                input={},
                input_hash=sha256(f"workspace-{import_id}".encode()).hexdigest(),
                idempotency_key=f"workspace-{uuid4().hex}",
                status="succeeded",
            )
        )
        await db.flush()
        db.add(
            WorkspaceVersion(
                id=workspace_id,
                repository_id=view.id,
                source_repository_id=view.id,
                cache_repository_id=cache_id,
                requested_by=user.id,
                idempotency_key=uuid4().hex,
                input_hash=sha256(f"workspace-input-{import_id}".encode()).hexdigest(),
                selection={"kind": "branch", "name": "main"},
                source={
                    "selection": {"kind": "branch", "name": "main"},
                    "ref": "refs/heads/main",
                    "source_repository_id": view.github_repository_id,
                    "transfer_repository_id": cache_id,
                },
                scopes=scopes,
                commit_sha=sha,
                tree_oid="c" * 40,
                status="ready",
                pin_ref="refs/devflow/pins/" + uuid4().hex,
                job_id=workspace_job_id,
            )
        )
        corpus = await db.get(KnowledgeCorpus, view.id)
        if corpus is None:
            corpus = KnowledgeCorpus(repository_id=view.id)
            db.add(corpus)
        corpus.target_generation = generation
        corpus.target_workspace_id = None
        corpus.target_sha = sha
        corpus.path_rule_version = "knowledge-paths-v1"
        corpus.path_scopes = {}
        db.add(
            Job(
                id=import_job_id,
                kind="knowledge.import",
                repository_id=view.id,
                requested_by=user.id,
                input={
                    "import_id": str(import_id),
                    "target_generation": generation,
                },
                input_hash=sha256(f"{import_id}:{generation}".encode()).hexdigest(),
                idempotency_key=f"import-job-{uuid4().hex}",
                status="succeeded",
            )
        )
        await db.flush()
        db.add(
            KnowledgeImport(
                id=import_id,
                repository_id=view.id,
                source_repository_id=view.id,
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
                job_id=import_job_id,
                status="ready",
                created_at=now,
                deadline=now + timedelta(hours=24),
                cursor=len(entries),
                included=len(entries),
                skipped=0,
                unsupported=0,
                over_limit=0,
                scanned_bytes=sum(len(body) for _, body, *_ in entries),
                failures=0,
                unscanned=False,
                complete=True,
                composition_version=1,
                target_workspace_id=None,
                target_sha=sha,
                target_path_scopes={},
                uploads_after=None,
                uploads_done=True,
                workspace_cursor=len(entries),
                workspace_done=True,
                eligible_files=len(entries),
                processed_chunks=sum(len(drafts) for _, _, _, _, drafts in entries),
                tokenizer_fingerprint=fingerprint,
                chunking_version=chunking_version,
            )
        )
        await db.flush()
        for ordinal, (relative, body, document_id, identity, drafts) in enumerate(
            entries
        ):
            db.add(
                Document(
                    id=document_id,
                    repository_id=view.id,
                    source_repository_id=view.id,
                    kind="workspace",
                    path=relative,
                    attributes={},
                )
            )
            revision = DocumentRevision(
                id=uuid4(),
                document_id=document_id,
                revision_hash=identity.revision_hash,
                source_version=workspace_source_version(
                    repository_id=view.id,
                    source_repository_id=view.id,
                    dependency_repository_ids=(view.id,),
                    workspace_id=workspace_id,
                    sha=sha,
                    path=relative,
                ),
                workspace_id=workspace_id,
                sha=sha,
                body=body,
                body_bytes=len(body),
                searchable=True,
                availability="available",
                published_at=now,
            )
            db.add(revision)
            await db.flush()
            db.add(RevisionDependency(revision_id=revision.id, repository_id=view.id))
            db.add(
                ImportMember(
                    id=uuid4(),
                    import_id=import_id,
                    source_key=relative,
                    ordinal=ordinal,
                    revision_id=revision.id,
                    status="included",
                    member_kind="workspace",
                    next_chunk=len(drafts),
                    total_chunks=len(drafts),
                    declared_bytes=len(body),
                )
            )
            for chunk_ordinal, draft in enumerate(drafts):
                db.add(
                    Chunk(
                        id=uuid4(),
                        revision_id=revision.id,
                        ordinal=chunk_ordinal,
                        text=draft.text,
                        embedding_text=draft.embedding_text,
                        content_hash=draft.content_hash,
                        token_count=draft.token_count,
                        tokenizer_fingerprint=draft.tokenizer_fingerprint,
                        chunking_version=draft.chunking_version,
                        start_line=draft.location.start_line,
                        end_line=draft.location.end_line,
                        start_byte=draft.location.start_byte,
                        end_byte=draft.location.end_byte,
                        attributes={},
                    )
                )
        db.add(
            ImportRepositoryEpoch(
                import_id=import_id,
                repository_id=view.id,
                installation_id=repository_row.installation_id,
                user_version=user.auth_version,
                installation_version=installation_row.auth_version,
                repository_version=repository_row.auth_version,
            )
        )
        await db.flush()
    return import_id


async def publish_import(env, index, inference, import_id):
    build = IndexBuilder(
        KnowledgeService(env.factory, env.policy),
        inference=inference,
        index=index,
    )
    receipt = await build.request(env.actor, import_id, f"quality-{uuid4().hex}")
    await env.due(receipt.job_id)
    for _ in range(BATCH_BUDGET):
        lease = await Queue(env.factory, lease_seconds=600).claim(
            uuid4(), allowed_kinds=MODEL_JOB_KINDS
        )
        assert lease is not None, "index build produced no claimable Job"
        await build.run_batch(lease)
        async with env.factory() as db:
            row = await db.get(IndexVersion, receipt.index_id)
        assert row is not None
        if row.status in {"ready", "retired"}:
            return row
        assert row.status in {"queued", "building"}, row.status
    raise AssertionError("index build did not finish within the batch budget")


def selection_for(question, view_ids) -> QuerySelection:
    return QuerySelection(
        selected_repository_ids=tuple(
            view_ids[name] for name in question["selected_repositories"]
        ),
        indexes=tuple(
            IndexSelection(
                repository_id=view_ids[item["repository"]],
                sha=item["sha"],
                target=item["target"],
            )
            for item in question["indexes"]
        ),
    )


def matches(chunks, names) -> list[RetrievalMatch]:
    return [
        RetrievalMatch(
            repository=names[chunk.identity.repository_id],
            sha=chunk.identity.sha,
            path=chunk.identity.path,
            start_line=chunk.location.start_line,
            end_line=chunk.location.end_line,
            text=chunk.text,
        )
        for chunk in chunks
    ]


def evidence_matches(evidence, names) -> list[RetrievalMatch]:
    return [
        RetrievalMatch(
            repository=names[item.identity.repository_id],
            sha=item.identity.sha,
            path=item.identity.path,
            start_line=item.location.start_line,
            end_line=item.location.end_line,
            text=item.text,
        )
        for item in evidence
    ]


def gap_report(report) -> str:
    rows = [
        f"{name}: recall={group.macro_recall} ndcg={group.macro_ndcg} "
        f"false_hits={group.false_hits} abstentions={group.abstentions} "
        f"leaks={group.leaks}"
        for name, group in sorted(report.groups.items())
    ]
    missed = [
        f"{item.id}({item.group}) recall={item.recall}"
        for item in report.questions
        if item.answerable and (item.recall or 0) < 1
    ]
    return "\n".join(rows) + "\nmissed: " + ", ".join(missed[:24])


async def test_fixed_set_quality_on_real_models(
    knowledge_env, workspace_tmp_path, record_property
):
    env = knowledge_env
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    validate_quality(gold, root=FIXTURES)
    views = await register_repositories(env, gold)
    settings = runtime_settings(workspace_tmp_path)
    index, client = make_index(settings)
    inference = LocalInference(settings)
    try:
        model = await inference.start()
        published = {}
        for name, config in gold["repositories"].items():
            import_id = await seed_import(
                env,
                views[name],
                root=FIXTURES / config["root"],
                sha=config["sha"],
                generation=config["index_generation"],
                cache_id=100 + len(published),
            )
            row = await publish_import(env, index, inference, import_id)
            assert row.status == "ready" and row.queryable
            published[name] = row
        # Moving the corpus target forward keeps the earlier published version
        # explicitly selectable as an older "active" index.
        for name, config in gold["repositories"].items():
            target_sha = config.get("target_sha")
            if not target_sha:
                continue
            async with env.factory.begin() as db:
                corpus = await db.get(KnowledgeCorpus, views[name].id)
                assert corpus is not None
                corpus.target_generation = config["target_generation"]
                corpus.target_sha = target_sha
        service = RetrievalService(
            env.factory, env.policy, inference=inference, index=index
        )
        names = {view.id: name for name, view in views.items()}
        view_ids = {name: view.id for name, view in views.items()}
        scopes = {}
        results = {method: {} for method in METHODS}
        questions = gold["questions"]
        vectors = await inference.embed([item["question"] for item in questions])
        assert len(vectors) == len(questions)
        for question, vector in zip(questions, vectors, strict=True):
            selection = selection_for(question, view_ids)
            key = selection.model_dump_json()
            scope = scopes.get(key)
            if scope is None:
                scope = await service.freeze(env.actor, selection, fresh=True)
                scopes[key] = scope
            filters = tuple(
                IndexFilter(item.repository_id, item.index_version_id)
                for item in scope.indexes
            )
            found = await asyncio.to_thread(
                index.search,
                question["question"],
                vector,
                filters,
                limit=SEARCH_LIMIT,
            )
            for method, fused in (
                ("bm25", fuse(found.bm25, ())),
                ("dense", fuse((), found.dense)),
                ("rrf", fuse(found.bm25, found.dense)),
            ):
                authorized, dropped = await service._authorized(scope, fused)
                selected = select_evidence(authorized, limit=K)
                results[method][question["id"]] = matches(selected, names)
                assert dropped == 0, f"{question['id']} {method} dropped {dropped} rows"
            evidence_set = await service.retrieve(
                RetrievalContext(
                    lease=Lease(uuid4(), uuid4(), 1, 1),
                    scope=scope,
                    deadline=datetime.now(UTC) + timedelta(seconds=600),
                ),
                question["question"],
            )
            results["rrf_rerank"][question["id"]] = evidence_matches(
                evidence_set.evidence, names
            )
        reports = {method: score_questions(gold, results[method]) for method in METHODS}
        for method, report in reports.items():
            assert report.leaks == 0, (
                f"{method} leaked unselected content\n{gap_report(report)}"
            )
        primary = reports["rrf_rerank"]
        assert primary.macro_recall_at_6 is not None
        assert primary.macro_ndcg_at_6 is not None
        assert primary.macro_recall_at_6 >= TARGET, gap_report(primary)
        assert primary.macro_ndcg_at_6 >= TARGET, gap_report(primary)
        record = {
            "fixture": gold["version"],
            "model": asdict(model),
            "indexes": {
                name: {
                    "index_version_id": str(row.id),
                    "fingerprint": row.fingerprint,
                    "target_sha": row.target_sha,
                    "member_digest": row.member_digest,
                }
                for name, row in published.items()
            },
            "reports": {method: report.as_dict() for method, report in reports.items()},
        }
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False)
        print("QUALITY_REPORT " + encoded)
        record_property("quality_report", encoded)
        record_property("model_fingerprint", json.dumps(asdict(model), sort_keys=True))
        record_property(
            "index_fingerprints",
            json.dumps(
                {name: row.fingerprint for name, row in published.items()},
                sort_keys=True,
            ),
        )
    finally:
        with suppress(Exception):
            await inference.drain(timeout=300)
        close_index(client, settings.milvus_collection)
