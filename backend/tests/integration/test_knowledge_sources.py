"""Knowledge identity/admission uses real PostgreSQL and repository policy."""

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414


def test_knowledge_contract_source_identity():
    from devflow.knowledge.schemas import ChunkLocation, SourceIdentity

    repo, source = uuid4(), uuid4()
    values = {
        "repository_id": repo,
        "source_repository_id": source,
        "dependency_repository_ids": (repo, source),
        "document_id": uuid4(),
        "revision_hash": "a" * 64,
        "workspace_id": uuid4(),
        "sha": "b" * 40,
        "path": "docs/Case.md",
    }
    assert SourceIdentity(**values).path == "docs/Case.md"
    for changed in (
        {"sha": "abc"},
        {"revision_hash": "abc"},
        {"path": "../private"},
        {"path": "x" * 4097},
        {"source_repository_id": 123},
        {"dependency_repository_ids": (repo,)},
        {"workspace_id": None},
    ):
        with pytest.raises(ValidationError):
            SourceIdentity(**(values | changed))
    assert ChunkLocation(start_line=1, end_line=1, start_byte=0, end_byte=3)
    with pytest.raises(ValidationError):
        ChunkLocation(start_line=2, end_line=1, start_byte=4, end_byte=3)


def test_knowledge_contract_import_request():
    from devflow.knowledge.schemas import ImportRequest

    adapter = TypeAdapter(ImportRequest)
    values = {
        "kind": "upload",
        "repository_id": uuid4(),
        "filename": "note.md",
        "staging_id": uuid4(),
    }
    assert adapter.validate_python(values).document_id is None
    for changed in (
        {"filename": "note.rst"},
        {"filename": "../note.md"},
        {"filename": "https://example.test/note.md"},
        {"url": "https://example.test"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(values | changed)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {k: v for k, v in values.items() if k != "repository_id"}
        )
    workspace = {
        "kind": "workspace",
        "repository_id": uuid4(),
        "workspace_id": uuid4(),
        "document_paths": ("docs",),
        "code_paths": ("src",),
    }
    assert adapter.validate_python(workspace).code_paths == ("src",)
    with pytest.raises(ValidationError):
        adapter.validate_python(workspace | {"code_paths": ("../src",)})
    with pytest.raises(ValidationError):
        adapter.validate_python(workspace | {"document_paths": (), "code_paths": ()})


@pytest.mark.parametrize("filename", ["md", "txt", "MD", "TXT"])
@pytest.mark.asyncio
async def test_upload_requires_actual_suffix(knowledge_env, filename):
    from devflow.errors import AppError
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    with pytest.raises(ValidationError):
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            filename=filename,
            staging_id=uuid4(),
        )
    with pytest.raises(AppError) as error:
        await KnowledgeService(env.factory, env.policy).stage_upload(
            env.actor, env.repository.id, filename, b"body"
        )
    assert error.value.status == 422


@pytest.mark.parametrize("identity_kind", ["document", "member"])
@pytest.mark.asyncio
async def test_long_exact_source_identity(knowledge_env, identity_kind):
    import random
    import string

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from devflow.knowledge.models import Document, ImportMember
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService
    from devflow.workspace.read import valid_path

    env = knowledge_env
    # Deterministic high-entropy input: TOAST compression cannot rescue a full
    # B-tree key. The keys differ near the end, so truncation would also fail.
    rng = random.Random(20261023)
    prefix = "docs/" + "".join(
        rng.choices(string.ascii_letters + string.digits, k=4087)
    )
    keys = (prefix + "A.md", prefix + "B.md")
    assert all(len(key.encode("utf-8")) == 4096 and valid_path(key) for key in keys)
    if identity_kind == "member":
        service = KnowledgeService(env.factory, env.policy)
        stage = await service.stage_upload(
            env.actor, env.repository.id, "note.md", b"x"
        )
        receipt = await service.request_import(
            env.actor,
            UploadImportRequest(
                kind="upload",
                repository_id=env.repository.id,
                filename="note.md",
                staging_id=stage,
            ),
            "long-identity",
        )

    def row(key, ordinal):
        if identity_kind == "document":
            return Document(
                repository_id=env.repository.id,
                source_repository_id=env.repository.id,
                kind="workspace",
                path=key,
            )
        return ImportMember(
            import_id=receipt.import_id, source_key=key, ordinal=ordinal
        )

    async with env.factory.begin() as db:
        db.add_all([row(key, ordinal) for ordinal, key in enumerate(keys, start=1)])
    async with env.factory() as db:
        if identity_kind == "document":
            stored = list(
                await db.scalars(
                    select(Document.path).where(Document.kind == "workspace")
                )
            )
        else:
            stored = list(
                await db.scalars(
                    select(ImportMember.source_key).where(ImportMember.ordinal > 0)
                )
            )
        assert set(stored) == set(keys)
    # A changed ordinal cannot bypass exact member-key identity uniqueness.
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as db:
            db.add(row(keys[0], 3))


@pytest.mark.asyncio
async def test_knowledge_grant_rejects_epoch_change(knowledge_env):
    from sqlalchemy import update

    from devflow.errors import AppError
    from devflow.knowledge.access import KnowledgeAccess
    from devflow.repositories.models import Repository

    env = knowledge_env
    other = await env.fork()
    access = KnowledgeAccess(env.policy)
    grant = await access.verify(env.actor, (other.id, env.repository.id), fresh=True)
    assert {e.repository_id for e in grant.repository_epochs} == {
        other.id,
        env.repository.id,
    }
    async with env.factory.begin() as db:
        await access.lock(db, grant)
    async with env.factory.begin() as db:
        await db.execute(
            update(Repository)
            .where(Repository.id == other.id)
            .values(installation_id=11)
        )
    with pytest.raises(AppError):
        async with env.factory.begin() as db:
            await access.lock(db, grant)


@pytest.mark.asyncio
async def test_knowledge_grant_requires_actual_source(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.access import KnowledgeAccess

    env = knowledge_env
    other = await env.fork()
    env.repositories[(12, 1)] = ([], None)
    with pytest.raises(AppError):
        await KnowledgeAccess(env.policy).verify(
            env.actor, (env.repository.id, other.id), fresh=True
        )


@pytest.mark.asyncio
async def test_target_generation_and_idempotency(knowledge_env):
    from sqlalchemy import func, select

    from devflow.errors import AppError
    from devflow.jobs.models import Job, RunEvent
    from devflow.knowledge.models import (
        DocumentRevision,
        KnowledgeCorpus,
        KnowledgeImport,
    )
    from devflow.knowledge.schemas import UploadImportRequest, WorkspaceImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    workspace = await env.workspace()
    first = await service.request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace", repository_id=env.repository.id, workspace_id=workspace.id
        ),
        "workspace",
    )
    stage = await service.stage_upload(
        env.actor, env.repository.id, "note.md", b"old\n"
    )
    request = UploadImportRequest(
        kind="upload",
        repository_id=env.repository.id,
        filename="note.md",
        staging_id=stage,
    )
    receipt = await service.request_import(env.actor, request, "upload")
    async with env.factory.begin() as db:
        from devflow.knowledge.models import UploadStage

        staged = await db.get(UploadStage, stage)
        old = await db.get(DocumentRevision, staged.revision_id)
        document_id, old_id = old.document_id, old.id
        old.published_at = await db.scalar(select(func.clock_timestamp()))
        old.searchable = True  # Simulate previously completed Task3 publication.
    replacement_stage = await service.stage_upload(
        env.actor, env.repository.id, "note.md", b"new\n", document_id=document_id
    )
    async with env.factory() as db:
        assert (await db.get(DocumentRevision, old_id)).searchable is True
    replacement = request.model_copy(
        update={"document_id": document_id, "staging_id": replacement_stage}
    )
    second = await service.request_import(env.actor, replacement, "replace")
    assert (
        second.target_generation
        == receipt.target_generation + 1
        == first.target_generation + 2
    )
    assert await service.request_import(env.actor, replacement, "replace") == second
    with pytest.raises(AppError) as error:
        await service.request_import(env.actor, replacement, "upload")
    assert error.value.code == "idempotency_conflict"
    async with env.factory() as db:
        assert (await db.get(DocumentRevision, old_id)).searchable is False
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        assert (
            corpus.target_sha == "b" * 40
            and corpus.target_generation == second.target_generation
        )
        job = await db.get(Job, second.job_id)
        assert job.kind == "knowledge.import" and job.requested_by == env.actor.id
        assert job.input == {
            "import_id": str(second.import_id),
            "target_generation": second.target_generation,
        }
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.job_id == job.id)
            )
            == 1
        )
        imported = await db.get(KnowledgeImport, second.import_id)
        assert imported.actor_auth_version == env.actor.auth_version
        assert (imported.deadline - imported.created_at).total_seconds() == 86400


@pytest.mark.asyncio
async def test_admin_without_repository_access_denied(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    env.repositories[(11, 1)] = ([], None)
    with pytest.raises(AppError):
        await KnowledgeService(env.factory, env.policy).stage_upload(
            env.actor, env.repository.id, "note.md", b"hello"
        )


@pytest.mark.asyncio
async def test_knowledge_staging_binding_and_limits(knowledge_env):
    from datetime import timedelta

    from sqlalchemy import func, select

    from devflow.errors import AppError
    from devflow.knowledge.models import (
        DocumentRevision,
        KnowledgeCapacity,
        UploadStage,
    )
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    first = await service.stage_upload(
        env.actor, env.repository.id, "note.txt", b"same"
    )
    async with env.factory() as db:
        original = await db.get(UploadStage, first)
        revision = await db.get(DocumentRevision, original.revision_id)
        document_id = revision.document_id
    second = await service.stage_upload(
        env.actor, env.repository.id, "note.txt", b"same", document_id=document_id
    )
    async with env.factory() as db:
        assert (await db.get(UploadStage, second)).revision_id == original.revision_id
        assert (await db.get(KnowledgeCapacity, 1)).body_bytes == 4
        assert not (await db.get(DocumentRevision, original.revision_id)).searchable
    request = UploadImportRequest(
        kind="upload",
        repository_id=env.repository.id,
        document_id=document_id,
        filename="note.txt",
        staging_id=first,
    )
    receipt = await service.request_import(env.actor, request, "stage")
    async with env.factory.begin() as db:
        row = await db.get(UploadStage, first)
        row.expires_at = await db.scalar(select(func.clock_timestamp())) - timedelta(
            days=1
        )
    assert await service.request_import(env.actor, request, "stage") == receipt
    with pytest.raises(AppError):
        await service.request_import(env.actor, request, "different-key")
    for filename, body in (
        ("note.rst", b"valid"),
        ("note.txt", b"\xff"),
        ("note.md", b"x" * (1048576 + 1)),
        ("note.txt", b"x\0y"),
    ):
        with pytest.raises(AppError):
            await service.stage_upload(env.actor, env.repository.id, filename, body)


@pytest.mark.asyncio
async def test_fork_identity_survives_dedup(knowledge_env):
    from sqlalchemy import select

    from devflow.errors import AppError
    from devflow.knowledge.models import (
        Document,
        DocumentRevision,
        ImportRepositoryEpoch,
        KnowledgeImport,
        RevisionDependency,
    )
    from devflow.knowledge.schemas import WorkspaceImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    source = await env.fork()
    base_version, fork_version = await env.workspace(), await env.workspace(source)
    service = KnowledgeService(env.factory, env.policy)
    receipts = [
        await service.request_import(
            env.actor,
            WorkspaceImportRequest(
                kind="workspace",
                repository_id=env.repository.id,
                workspace_id=version.id,
            ),
            str(index),
        )
        for index, version in enumerate((base_version, fork_version))
    ]
    async with env.factory() as db:
        base, fork = [
            await db.get(KnowledgeImport, receipt.import_id) for receipt in receipts
        ]
        assert base.sha == fork.sha == "b" * 40
        assert (
            base.source_repository_id == env.repository.id
            and fork.source_repository_id == source.id
        )
        dependencies = set(
            await db.scalars(
                select(ImportRepositoryEpoch.repository_id).where(
                    ImportRepositoryEpoch.import_id == fork.id
                )
            )
        )
        assert dependencies == {env.repository.id, source.id}
    # Materialization belongs to Task2/3. Persist its expected output here to
    # prove the schema does not merge identical canonical bytes across sources.
    revision_ids = []
    async with env.factory.begin() as db:
        for actual_source, version in (
            (env.repository.id, base_version),
            (source.id, fork_version),
        ):
            document = Document(
                repository_id=env.repository.id,
                source_repository_id=actual_source,
                kind="workspace",
                path="docs/shared.md",
            )
            db.add(document)
            await db.flush()
            revision = DocumentRevision(
                document_id=document.id,
                revision_hash=env.digest(b"same\n"),
                source_version=env.digest(str(version.id).encode()),
                workspace_id=version.id,
                sha=version.commit_sha,
                body=b"same\n",
                body_bytes=5,
            )
            db.add(revision)
            await db.flush()
            revision_ids.append(revision.id)
            db.add_all(
                [
                    RevisionDependency(revision_id=revision.id, repository_id=repo)
                    for repo in {env.repository.id, actual_source}
                ]
            )
    async with env.factory() as db:
        left, right = [await db.get(DocumentRevision, key) for key in revision_ids]
        assert left.id != right.id and left.revision_hash == right.revision_hash
        assert set(
            await db.scalars(
                select(RevisionDependency.repository_id).where(
                    RevisionDependency.revision_id == right.id
                )
            )
        ) == {env.repository.id, source.id}
    env.repositories[(12, 1)] = ([], None)
    with pytest.raises(AppError):
        await service.request_import(
            env.actor,
            WorkspaceImportRequest(
                kind="workspace",
                repository_id=env.repository.id,
                workspace_id=fork_version.id,
            ),
            "1",
        )


@pytest.mark.asyncio
async def test_knowledge_persistence_constraints(knowledge_env):
    from sqlalchemy.exc import IntegrityError

    from devflow.knowledge.models import (
        Chunk,
        Document,
        DocumentRevision,
        ImportMember,
        KnowledgeCapacity,
        RevisionDependency,
        RevisionPin,
        RevisionRetention,
        UploadStage,
    )
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", b"abc")
    receipt = await service.request_import(
        env.actor,
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            filename="note.md",
            staging_id=stage,
        ),
        "persist",
    )
    async with env.factory.begin() as db:
        revision_id = (await db.get(UploadStage, stage)).revision_id
        revision = await db.get(DocumentRevision, revision_id)
        document_id = revision.document_id
        db.add_all(
            [
                RevisionPin(revision_id=revision_id, expires_at=revision.created_at),
                RevisionRetention(
                    owner_kind="query",
                    owner_id=uuid4(),
                    revision_id=revision_id,
                    expires_at=revision.created_at,
                ),
                Chunk(
                    revision_id=revision_id,
                    ordinal=0,
                    text="abc",
                    embedding_text="abc",
                    content_hash=env.digest(b"abc"),
                    token_count=1,
                    tokenizer_fingerprint="d" * 64,
                    chunking_version="v1",
                    start_line=1,
                    end_line=1,
                    start_byte=0,
                    end_byte=3,
                ),
            ]
        )
    for row in (
        ImportMember(
            import_id=receipt.import_id,
            source_key=str(document_id),
            ordinal=0,
            revision_id=revision_id,
        ),
        RevisionDependency(revision_id=revision_id, repository_id=env.repository.id),
    ):
        with pytest.raises(IntegrityError):
            async with env.factory.begin() as db:
                db.add(row)
                await db.flush()
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as db:
            (await db.get(KnowledgeCapacity, 1)).body_bytes = 134217729
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as db:
            (await db.get(Document, document_id)).attributes = {"long": "中" * 3000}


@pytest.mark.asyncio
async def test_knowledge_request_rolls_back_atomically(knowledge_env, monkeypatch):
    from sqlalchemy import func, select

    import devflow.knowledge.service as module
    from devflow.jobs.models import Job
    from devflow.knowledge.models import KnowledgeCorpus, UploadStage
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", b"abc")

    async def fail_event(*args):
        raise RuntimeError("synthetic transactional failure")

    monkeypatch.setattr(module, "append_event", fail_event)
    with pytest.raises(RuntimeError, match="synthetic"):
        await service.request_import(
            env.actor,
            UploadImportRequest(
                kind="upload",
                repository_id=env.repository.id,
                filename="note.md",
                staging_id=stage,
            ),
            "rollback",
        )
    async with env.factory() as db:
        assert (await db.get(UploadStage, stage)).consumed_import_id is None
        corpus = await db.get(KnowledgeCorpus, env.repository.id)
        assert corpus is None or corpus.target_generation == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "knowledge.import")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_knowledge_import_is_claimed_with_real_handler(knowledge_env):
    from devflow.jobs.models import Job
    from devflow.jobs.queue import Queue
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", b"abc")
    receipt = await service.request_import(
        env.actor,
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            filename="note.md",
            staging_id=stage,
        ),
        "unhandled",
    )
    await env.due(receipt.job_id)
    lease = await Queue(env.factory, settings=env.settings).claim(uuid4())
    assert lease is not None and lease.job_id == receipt.job_id
    async with env.factory() as db:
        job = await db.get(Job, receipt.job_id)
        assert (job.status, job.attempt, job.last_sequence) == ("running", 1, 2)


@pytest.mark.asyncio
async def test_knowledge_pending_jobs_do_not_starve_supported_kind(
    knowledge_env, monkeypatch
):
    from datetime import timedelta

    from sqlalchemy import func, select

    from devflow.jobs.models import Job
    from devflow.jobs.queue import Queue

    env = knowledge_env
    probe = await env.jobs.submit_probe(env.actor, env.repository.id, "probe")
    import devflow.jobs.queue as queue_module

    # Simulate a deployment that has not enabled this kind; test pre-LIMIT filtering.
    monkeypatch.setattr(
        queue_module,
        "_EXECUTABLE_KINDS",
        tuple(
            kind
            for kind in queue_module._EXECUTABLE_KINDS
            if kind != "knowledge.import"
        ),
    )
    async with env.factory.begin() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        for _ in range(101):
            db.add(
                Job(
                    kind="knowledge.import",
                    requested_by=env.actor.id,
                    repository_id=env.repository.id,
                    idempotency_key=uuid4().hex,
                    input_hash="a" * 64,
                    input={"import_id": str(uuid4()), "target_generation": 1},
                    available_at=now - timedelta(days=2),
                )
            )
        (await db.get(Job, probe.id)).available_at = now - timedelta(days=1)
    lease = await Queue(env.factory, settings=env.settings).claim(uuid4())
    assert lease is not None and lease.job_id == probe.id


@pytest.mark.asyncio
async def test_knowledge_job_not_visible_through_probe_api(knowledge_env):
    from support.repository_env import browser

    from devflow.errors import AppError
    from devflow.jobs.events import JobEventService
    from devflow.jobs.models import Job
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", b"abc")
    receipt = await service.request_import(
        env.actor,
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            filename="note.md",
            staging_id=stage,
        ),
        "private",
    )
    for state in ("queued", "succeeded"):
        async with env.factory.begin() as db:
            job = await db.get(Job, receipt.job_id)
            job.status = state
            job.result = (
                {
                    "import_id": str(receipt.import_id),
                    "target_generation": 1,
                    "status": "ready",
                }
                if state == "succeeded"
                else None
            )
        with pytest.raises(AppError) as error:
            await env.jobs.get_visible(env.actor, receipt.job_id)
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await JobEventService(env.jobs).read_events(env.actor, receipt.job_id, 0)
        assert error.value.status == 404
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        for path in (
            f"/api/v1/jobs/{receipt.job_id}",
            f"/api/v1/jobs/{receipt.job_id}/events",
        ):
            response = await client.get(path)
            assert response.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_member_read_grant_and_current_role(knowledge_env):
    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.errors import AppError
    from devflow.knowledge.access import KnowledgeAccess
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    access = KnowledgeAccess(env.policy)
    grant = await access.verify(env.actor, (env.repository.id,), fresh=True)
    async with env.factory.begin() as db:
        (await db.get(User, env.actor.id)).role = "member"
    with pytest.raises(AppError):
        async with env.factory.begin() as db:
            await access.lock(db, grant)
    member = Actor(env.actor.id, env.actor.github_id, "member", env.actor.auth_version)
    member_grant = await access.verify(member, (env.repository.id,), fresh=True)
    async with env.factory.begin() as db:
        await access.lock(db, member_grant)
    with pytest.raises(AppError) as error:
        await KnowledgeService(env.factory, env.policy).stage_upload(
            member, env.repository.id, "note.md", b"abc"
        )
    assert error.value.status == 403


@pytest.mark.asyncio
async def test_knowledge_unpublished_revision_cannot_be_searchable(knowledge_env):
    from sqlalchemy.exc import IntegrityError

    from devflow.knowledge.models import DocumentRevision, UploadStage
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    stage = await KnowledgeService(env.factory, env.policy).stage_upload(
        env.actor, env.repository.id, "note.md", b"abc"
    )
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as db:
            revision = await db.get(
                DocumentRevision, (await db.get(UploadStage, stage)).revision_id
            )
            assert revision.published_at is None
            revision.searchable = True


@pytest.mark.asyncio
async def test_knowledge_capacity_and_concurrent_idempotency(knowledge_env):
    import asyncio

    from sqlalchemy import func, select

    from devflow.errors import AppError
    from devflow.knowledge.models import Document, KnowledgeCapacity, KnowledgeImport
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(
        env.actor, env.repository.id, "note.txt", b"x" * 1048576
    )
    request = UploadImportRequest(
        kind="upload",
        repository_id=env.repository.id,
        filename="note.txt",
        staging_id=stage,
    )
    first, second = await asyncio.gather(
        service.request_import(env.actor, request, "parallel"),
        service.request_import(env.actor, request, "parallel"),
    )
    assert first == second
    async with env.factory.begin() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeImport)) == 1
        capacity = await db.get(KnowledgeCapacity, 1)
        assert capacity.body_bytes == 1048576
        # Consistent admission-boundary counter fixture, not a 128 MiB load test.
        capacity.reserved_bytes = 134217728 - capacity.body_bytes - 1
    with pytest.raises(AppError) as error:
        await service.stage_upload(env.actor, env.repository.id, "other.md", b"xx")
    assert error.value.code == "storage_exhausted"
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Document)) == 1


@pytest.mark.asyncio
async def test_knowledge_capacity_initialization_rollback_and_concurrency(
    knowledge_env, monkeypatch
):
    import asyncio

    from sqlalchemy import func, select

    from devflow.knowledge.models import (
        KnowledgeCapacity,
        RepositoryCapacity,
        UploadStage,
    )
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    initialize = service._initialize_repository_capacity

    async def fail_after_initialization(db, repository_id):
        await initialize(db, repository_id)
        raise RuntimeError("injected rollback after capacity initialization")

    monkeypatch.setattr(
        service, "_initialize_repository_capacity", fail_after_initialization
    )
    with pytest.raises(RuntimeError, match="injected rollback"):
        await service.stage_upload(env.actor, env.repository.id, "note.md", b"one")
    async with env.factory() as db:
        assert await db.get(KnowledgeCapacity, 1) is None
        assert await db.get(RepositoryCapacity, env.repository.id) is None
    monkeypatch.setattr(service, "_initialize_repository_capacity", initialize)
    stages = await asyncio.gather(
        *[
            service.stage_upload(env.actor, env.repository.id, "note.md", body)
            for body in (b"one", b"two")
        ]
    )
    assert len(set(stages)) == 2
    async with env.factory() as db:
        assert (await db.get(KnowledgeCapacity, 1)).body_bytes == 6
        assert (
            await db.scalar(select(func.count()).select_from(RepositoryCapacity)) == 1
        )
        for stage_id in stages:
            stage = await db.get(UploadStage, stage_id)
            assert (stage.expires_at - stage.created_at).total_seconds() == 86400


@pytest.mark.asyncio
async def test_knowledge_rejects_foreign_expired_or_unknown_stage(knowledge_env):
    from datetime import timedelta

    from sqlalchemy import func, select

    from devflow.auth.models import User
    from devflow.errors import AppError
    from devflow.knowledge.models import UploadStage
    from devflow.knowledge.schemas import UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", b"abc")
    request = UploadImportRequest(
        kind="upload",
        repository_id=env.repository.id,
        filename="note.md",
        staging_id=stage,
    )
    with pytest.raises(AppError):
        await service.request_import(
            env.actor, request.model_copy(update={"staging_id": uuid4()}), "unknown"
        )
    with pytest.raises(AppError):
        await service.request_import(
            env.actor,
            request.model_copy(update={"document_id": uuid4()}),
            "foreign-document",
        )
    async with env.factory.begin() as db:
        foreign = User(github_id=99, role="admin")
        db.add(foreign)
        await db.flush()
        (await db.get(UploadStage, stage)).actor_id = foreign.id
    with pytest.raises(AppError):
        await service.request_import(env.actor, request, "foreign-actor")
    async with env.factory.begin() as db:
        stored = await db.get(UploadStage, stage)
        stored.actor_id = env.actor.id
        stored.expires_at = await db.scalar(select(func.clock_timestamp())) - timedelta(
            days=1
        )
    with pytest.raises(AppError):
        await service.request_import(env.actor, request, "expired")
