"""Canonical and first-import reads over real dedicated PostgreSQL/policy."""

import asyncio
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from test_workspace_reads import MemoryCache


async def uploaded(env, body=b"body", *, publish=True):
    from devflow.knowledge.models import DocumentRevision, ImportMember, KnowledgeImport
    from devflow.knowledge.schemas import SourceIdentity, UploadImportRequest
    from devflow.knowledge.service import KnowledgeService

    service = KnowledgeService(env.factory, env.policy)
    stage = await service.stage_upload(env.actor, env.repository.id, "note.md", body)
    receipt = await service.request_import(
        env.actor,
        UploadImportRequest(
            kind="upload",
            repository_id=env.repository.id,
            staging_id=stage,
            filename="note.md",
        ),
        uuid4().hex,
    )
    async with env.factory.begin() as db:
        member = await db.scalar(
            select(ImportMember).where(ImportMember.import_id == receipt.import_id)
        )
        rev = await db.get(DocumentRevision, member.revision_id)
        if publish:
            rev.published_at = await db.scalar(select(func.clock_timestamp()))
            rev.searchable = True
            member.status = "included"
            (await db.get(KnowledgeImport, receipt.import_id)).status = "ready"
        identity = SourceIdentity(
            repository_id=env.repository.id,
            source_repository_id=env.repository.id,
            dependency_repository_ids=(env.repository.id,),
            document_id=rev.document_id,
            revision_hash=env.digest(body),
            workspace_id=None,
            sha=None,
            path=None,
        )
    return receipt, identity


def reader(env, cache=None):
    from devflow.knowledge.sources import SourceReader
    from devflow.pagination import CursorCodec
    from devflow.workspace.read import WorkspaceReader

    return SourceReader(
        env.factory,
        env.policy,
        WorkspaceReader(env.factory, env.policy, cache) if cache else None,
        CursorCodec(b"s" * 32),
    )


@pytest.mark.asyncio
async def test_preview_long_utf8_line_cursor_and_pin_release(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.models import RevisionPin

    env = knowledge_env
    body = ("中🙂" * 60000 + "\r\nlast").encode()
    _, identity = await uploaded(env, body)
    service = reader(env)
    pieces, cursor = [], None
    while True:
        view = await service.preview(env.actor, identity, line_count=1, cursor=cursor)
        assert view.is_current and len(view.text.encode()) <= 262144
        assert view.location.start_line == view.location.end_line == 1
        assert (
            body[view.location.start_byte : view.location.end_byte].decode()
            == view.text
        )
        pieces.append(view.text)
        cursor = view.next_cursor
        if not cursor:
            break
        with pytest.raises(AppError, match="Invalid"):
            await service.preview(env.actor, identity, line_count=2, cursor=cursor)
    assert "".join(pieces).encode() == body[: body.index(b"\n") + 1]
    assert view.partial  # There is still line 2, outside the bound original window.
    async with env.factory() as db:
        assert not list(await db.scalars(select(RevisionPin)))


@pytest.mark.asyncio
async def test_staged_bytes_only_readable_by_bound_import(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.models import UploadStage

    env = knowledge_env
    receipt, identity = await uploaded(env, b"original", publish=False)
    service = reader(env)
    for call in (service.preview, service.read):
        with pytest.raises(AppError) as error:
            await call(env.actor, identity)
        assert error.value.status == 404
    # The consumed stage was an admission credential; its old expiry cannot reset
    # or replace the real admitted import's fixed deadline.
    async with env.factory.begin() as db:
        stage = await db.scalar(select(UploadStage))
        stage.expires_at = await db.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    assert await service.read_import(env.actor, receipt.import_id) == b"original"
    with pytest.raises(AppError):
        await service.read_import(env.actor, receipt.import_id, path="docs/a.md")


@pytest.mark.asyncio
async def test_canonical_unavailable_empty_and_historical(knowledge_env):
    from devflow.knowledge.models import DocumentRevision, KnowledgeCorpus

    env = knowledge_env
    _, identity = await uploaded(env, b"")
    service = reader(env)
    view = await service.preview(env.actor, identity)
    assert view.text == "" and view.location is None and view.is_current
    async with env.factory.begin() as db:
        (await db.get(KnowledgeCorpus, env.repository.id)).target_generation += 1
    assert not (await service.preview(env.actor, identity)).is_current
    for state, reason in (
        ("expired", "source_expired"),
        ("deleted", "source_unavailable"),
    ):
        async with env.factory.begin() as db:
            revision = await db.scalar(select(DocumentRevision))
            revision.searchable = False
            revision.availability, revision.body = state, None
        view = await service.preview(env.actor, identity)
        assert view.reason == reason and view.text is None
        assert view.location is None and view.next_cursor is None and not view.partial


@pytest.mark.asyncio
async def test_raw_read_not_preview(knowledge_env, workspace_tmp_path):
    from devflow.knowledge.schemas import WorkspaceImportRequest
    from devflow.knowledge.service import KnowledgeService

    env = knowledge_env
    version = await env.workspace()
    cache = MemoryCache(workspace_tmp_path)
    body = ("中" * 100000).encode()
    cache.add(b"docs/large.md", body)
    receipt = await KnowledgeService(env.factory, env.policy).request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace", repository_id=env.repository.id, workspace_id=version.id
        ),
        uuid4().hex,
    )
    service = reader(env, cache)
    assert (
        await service.read_import(env.actor, receipt.import_id, path="docs/large.md")
        == body
    )
    view = await service.workspace.file(
        env.actor, env.repository.id, version.id, "docs/large.md"
    )
    assert view.text == "" and view.start_line is None and view.truncated
    assert cache.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,mode,reason",
    [
        (b"x", "120000", "symlink"),
        (b"x", "160000", "gitlink"),
        (b"\xff", "100644", "non_utf8"),
        (b"x\0", "100644", "binary"),
        (b"x" * 1048577, "100644", "too_large"),
        (
            b"version https://git-lfs.github.com/spec/v1\noid sha256:"
            + b"a" * 64
            + b"\nsize 42\n",
            "100644",
            "lfs_pointer",
        ),
    ],
    ids=["symlink", "gitlink", "non_utf8", "binary", "too_large", "lfs_pointer"],
)
async def test_raw_source_rejects_unsupported_items(
    knowledge_env, workspace_tmp_path, body, mode, reason
):
    from devflow.errors import AppError
    from devflow.workspace.read import WorkspaceReader

    env = knowledge_env
    version = await env.workspace()
    cache = MemoryCache(workspace_tmp_path)
    cache.add(b"docs/a.md", body, mode)
    with pytest.raises(AppError) as caught:
        await WorkspaceReader(env.factory, env.policy, cache).raw(
            env.actor,
            env.repository.id,
            version.id,
            "docs/a.md",
            sha=version.commit_sha,
        )
    assert caught.value.code == "validation_error"
    assert caught.value.reason == reason


@pytest.mark.asyncio
async def test_raw_read_keeps_shared_four_slot_admission(
    knowledge_env, workspace_tmp_path
):
    from devflow.errors import AppError
    from devflow.workspace.guards import reserve_guard, revoke_reservation
    from devflow.workspace.read import WorkspaceReader

    env = knowledge_env
    version = await env.workspace()
    cache = MemoryCache(workspace_tmp_path)
    cache.add(b"docs/a.md")
    guards = []
    try:
        for _ in range(4):
            guards.append(
                await reserve_guard(
                    env.factory,
                    env.settings,
                    version.id,
                    kind="read",
                    actor=env.actor,
                    deadline=time.monotonic() + 5,
                    lock_path=cache.lock_path(101),
                )
            )
        with pytest.raises(AppError) as caught:
            await WorkspaceReader(env.factory, env.policy, cache).raw(
                env.actor,
                env.repository.id,
                version.id,
                "docs/a.md",
                sha=version.commit_sha,
            )
        assert caught.value.code == "workspace_busy" and not cache.calls
    finally:
        for guard in guards:
            await revoke_reservation(env.factory, guard)


@pytest.mark.asyncio
async def test_pinned_preview_denies_delete_and_releases_on_cancel(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.models import Document, RevisionPin

    env = knowledge_env
    _, identity = await uploaded(env)
    service = reader(env)
    materialize = service._materialize

    async def delete_after_pin(revision_id):
        async with env.factory.begin() as db:
            pins = list(await db.scalars(select(RevisionPin)))
            assert len(pins) == 1 and pins[0].revision_id == revision_id
            now = await db.scalar(select(func.clock_timestamp()))
            assert 0 < (pins[0].expires_at - now).total_seconds() <= 30
            document = await db.get(Document, identity.document_id)
            document.deleted, document.accepted_revision_id = True, None
        return await materialize(revision_id)

    service._materialize = delete_after_pin
    with pytest.raises(AppError) as caught:
        await service.preview(env.actor, identity)
    assert caught.value.code == "version_changed"
    async with env.factory.begin() as db:
        assert not list(await db.scalars(select(RevisionPin)))
        (await db.get(Document, identity.document_id)).deleted = False
    entered = asyncio.Event()

    async def wait_after_pin(revision_id):
        entered.set()
        await asyncio.Event().wait()

    service._materialize = wait_after_pin
    task = asyncio.create_task(service.preview(env.actor, identity))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with env.factory() as db:
        assert not list(await db.scalars(select(RevisionPin)))


@pytest.mark.asyncio
async def test_import_rechecks_original_fork_epochs_after_raw(
    knowledge_env, workspace_tmp_path
):
    from devflow.errors import AppError
    from devflow.knowledge.schemas import WorkspaceImportRequest
    from devflow.knowledge.service import KnowledgeService
    from devflow.repositories.models import Repository

    env = knowledge_env
    fork = await env.fork()
    version = await env.workspace(fork)
    cache = MemoryCache(workspace_tmp_path)
    cache.add(b"docs/a.md", b"fork body")
    receipt = await KnowledgeService(env.factory, env.policy).request_import(
        env.actor,
        WorkspaceImportRequest(
            kind="workspace", repository_id=env.repository.id, workspace_id=version.id
        ),
        uuid4().hex,
    )
    service = reader(env, cache)
    original = service.workspace.raw

    async def revoke_after_raw(*args, **kwargs):
        body = await original(*args, **kwargs)
        async with env.factory.begin() as db:
            (await db.get(Repository, fork.id)).auth_version += 1
        return body

    service.workspace.raw = revoke_after_raw
    with pytest.raises(AppError):
        await service.read_import(env.actor, receipt.import_id, path="docs/a.md")


@pytest.mark.asyncio
async def test_preview_cursor_expiry_tampering_and_actor_binding(knowledge_env):
    from dataclasses import replace

    from devflow.errors import AppError

    env = knowledge_env
    _, identity = await uploaded(env, b"x" * 300000)
    service = reader(env)
    view = await service.preview(env.actor, identity)
    cursor = view.next_cursor
    assert cursor
    for other_actor, token in (
        (env.actor, cursor[:-1] + ("A" if cursor[-1] != "A" else "B")),
        (replace(env.actor, id=uuid4()), cursor),
    ):
        with pytest.raises(AppError) as caught:
            await service.preview(other_actor, identity, cursor=token)
        assert caught.value.code == "invalid_cursor"
    # Re-sign a controlled expired cursor: HMAC and original window remain valid.
    import hashlib
    import json

    scope = [identity.model_dump(mode="json"), 1, 500]
    purpose = (
        "knowledge-preview:v1:"
        + hashlib.sha256(
            json.dumps(
                scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
    for position in (
        {"offset": 262144, "expires": 1},
        {"offset": 262144, "expires": True},
    ):
        expired = service.codec.encode_cursor(env.actor.id, purpose, position, 1)
        with pytest.raises(AppError) as caught:
            await service.preview(env.actor, identity, cursor=expired)
        assert caught.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_target_change_during_preview_is_not_reported_current(knowledge_env):
    from devflow.errors import AppError
    from devflow.knowledge.models import KnowledgeCorpus

    env = knowledge_env
    _, identity = await uploaded(env)
    service = reader(env)
    materialize = service._materialize

    async def advance_after_pin(revision_id):
        body = await materialize(revision_id)
        async with env.factory.begin() as db:
            (await db.get(KnowledgeCorpus, env.repository.id)).target_generation += 1
        return body

    service._materialize = advance_after_pin
    with pytest.raises(AppError) as caught:
        await service.preview(env.actor, identity)
    assert caught.value.code == "version_changed"


@pytest.mark.asyncio
async def test_preview_rejects_empty_cursor(knowledge_env):
    from devflow.errors import AppError

    env = knowledge_env
    _, identity = await uploaded(env)
    with pytest.raises(AppError) as caught:
        await reader(env).preview(env.actor, identity, cursor="")
    assert caught.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_member_can_preview_but_cannot_read_unpublished_import(knowledge_env):
    from dataclasses import replace

    from devflow.auth.models import User
    from devflow.errors import AppError

    env = knowledge_env
    receipt, identity = await uploaded(env)
    async with env.factory.begin() as db:
        (await db.get(User, env.actor.id)).role = "member"
    member = replace(env.actor, role="member")
    view = await reader(env).preview(member, identity)
    assert view.text == "body"
    with pytest.raises(AppError) as caught:
        await reader(env).read_import(member, receipt.import_id)
    assert caught.value.status == 403


@pytest.mark.asyncio
async def test_workspace_canonical_read_uses_exact_published_identity(
    knowledge_env, workspace_tmp_path
):
    from devflow.errors import AppError
    from devflow.knowledge.models import Document, DocumentRevision, RevisionDependency
    from devflow.knowledge.schemas import SourceIdentity
    from devflow.knowledge.sources import workspace_source_version

    env = knowledge_env
    version = await env.workspace()
    cache = MemoryCache(workspace_tmp_path)
    body = b"original Git bytes\r\n"
    cache.add(b"docs/a.md", body)
    values = {
        "repository_id": env.repository.id,
        "source_repository_id": env.repository.id,
        "dependency_repository_ids": (env.repository.id,),
        "workspace_id": version.id,
        "sha": version.commit_sha,
        "path": "docs/a.md",
    }
    async with env.factory.begin() as db:
        document = Document(
            repository_id=env.repository.id,
            source_repository_id=env.repository.id,
            kind="workspace",
            path="docs/a.md",
        )
        db.add(document)
        await db.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_hash=env.digest(body),
            source_version=workspace_source_version(**values),
            workspace_id=version.id,
            sha=version.commit_sha,
            body=body,
            body_bytes=len(body),
            published_at=await db.scalar(select(func.clock_timestamp())),
        )
        db.add(revision)
        await db.flush()
        db.add(
            RevisionDependency(revision_id=revision.id, repository_id=env.repository.id)
        )
        identity = SourceIdentity(
            **values, document_id=document.id, revision_hash=env.digest(body)
        )
    service = reader(env, cache)
    result = await service.read(env.actor, identity)
    assert result.body == body and result.identity == identity
    view = await service.preview(env.actor, identity)
    assert view.text.encode() == body and not view.is_current
    with pytest.raises(AppError) as caught:
        await service.preview(
            env.actor, identity.model_copy(update={"path": "docs/A.md"})
        )
    assert caught.value.code == "version_changed"
