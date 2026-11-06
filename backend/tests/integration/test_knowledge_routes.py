"""Knowledge transport over real authorization, staging and admission."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.index_env import make_index, ready_import, set_active
from support.knowledge_env import job_env as job_env  # noqa: PLC0414
from support.knowledge_env import knowledge_env as knowledge_env  # noqa: PLC0414
from support.knowledge_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser

from devflow.auth.models import Session, User
from devflow.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeCorpus,
    KnowledgeImport,
    RevisionDependency,
    UploadStage,
)
from devflow.knowledge.sources import workspace_source_version

pytestmark = pytest.mark.asyncio


async def session_for(env, user):
    from sqlalchemy import func, text

    raw = f"synthetic-session-{uuid4()}"
    async with env.factory.begin() as db:
        db.add(
            Session(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                auth_version=user.auth_version,
                expires_at=func.clock_timestamp() + text("interval '8 hours'"),
            )
        )
    return raw


async def headers(env, client, *, key=None):
    current = (await client.get("/api/v1/session")).json()
    value = {
        "Origin": env.settings.public_base_url,
        "X-CSRF-Token": current["csrf_token"],
    }
    if key is not None:
        value["Idempotency-Key"] = key
    return value


async def stage(
    env,
    client,
    *,
    filename="note.md",
    body=b"# hello\n",
    key="stage-1",
    document_id=None,
):
    query = f"?filename={filename}"
    if document_id is not None:
        query += f"&document_id={document_id}"
    return await client.post(
        f"/api/v1/repositories/{env.repository.id}/knowledge/uploads{query}",
        content=body,
        headers=await headers(env, client, key=key),
    )


async def count(env, model):
    async with env.factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def published_upload(env, revisions, *, filename="note.md"):
    """Insert one upload document with published history, newest last.

    Each item is ``(availability, body)``. Direct rows keep pagination and
    reclamation scenarios independent of the ingestion pipeline while still
    exercising the real read projection, constraints and source reader.
    """
    async with env.factory.begin() as db:
        document = Document(
            repository_id=env.repository.id,
            source_repository_id=env.repository.id,
            kind="upload",
            attributes={"filename": filename},
        )
        db.add(document)
        await db.flush()
        now = await db.scalar(select(func.clock_timestamp()))
        ids = []
        for index, (availability, body) in enumerate(revisions):
            # A reclaimed revision has no body left, but its identity digest is
            # what the revision row recorded when the body was published.
            material = body if body is not None else f"{filename}:{index}".encode()
            digest = hashlib.sha256(material).hexdigest()
            revision = DocumentRevision(
                document_id=document.id,
                revision_hash=digest,
                source_version=digest,
                body=body,
                body_bytes=0 if body is None else len(body),
                availability=availability,
                published_at=now + timedelta(seconds=index),
            )
            db.add(revision)
            await db.flush()
            db.add(
                RevisionDependency(
                    revision_id=revision.id, repository_id=env.repository.id
                )
            )
            ids.append(revision.id)
        return document.id, ids


async def test_overview_requires_repository_access_and_returns_limits(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge"
        )
        assert response.status_code == 200
        view = response.json()
        assert view["repository_id"] == str(env.repository.id)
        assert view["target_generation"] == 0
        assert view["corpus"] is None
        assert view["needs_reimport"] is False
        assert view["index_status"] == "not_indexed"
        assert view["index_reason"] == "no_target"
        assert view["documents"] == {"items": [], "next_cursor": None}
        assert view["limits"] == {
            "max_files": 2000,
            "max_scan_bytes": 33554432,
            "max_file_bytes": 1048576,
        }
        assert view["path_rule_version"] == "knowledge-paths-v1"
        assert response.headers["cache-control"] == "private, no-store"

        denied = await client.get(f"/api/v1/repositories/{uuid4()}/knowledge")
        assert denied.status_code == 404
        assert denied.headers["cache-control"] == "private, no-store"


class ProbeIndex:
    """Only the read-path storage probe is replaced; DB state stays real."""

    def __init__(self, reason=None):
        self.reason = reason
        self.calls = []

    def probe(self, fingerprint, index_id, chunk_ids, *, expected_count=None):
        self.calls.append((fingerprint, index_id, tuple(chunk_ids), expected_count))
        return self.reason


async def test_overview_verifies_published_storage_before_reporting_ready(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    row = await make_index(env, fixture, status="ready")
    await set_active(env, row.id)
    base = f"/api/v1/repositories/{env.repository.id}/knowledge"
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)

        # The database still points at a ready version, but the vectors did
        # not survive the Milvus restore: never serve a ready projection.
        app.state.knowledge_index = ProbeIndex("collection_missing")
        missing = await client.get(base)
        assert missing.status_code == 409
        assert missing.json()["error"]["code"] == "index_not_ready"
        probe = app.state.knowledge_index
        assert len(probe.calls) == 1
        stored, index_id, members, expected = probe.calls[0]
        assert stored.embedding
        assert index_id == row.id
        assert len(members) == expected == 2

        app.state.knowledge_index = ProbeIndex(None)
        ready = await client.get(base)
        assert ready.status_code == 200
        view = ready.json()
        assert view["index_status"] == "ready"
        assert view["index_reason"] is None
        assert len(app.state.knowledge_index.calls) == 1


async def test_overview_exposes_non_ready_reason_without_storage_probe(
    knowledge_env,
):
    env = knowledge_env
    fixture = await ready_import(env, chunk_count=2)
    row = await make_index(env, fixture, status="ready")
    await set_active(env, row.id)
    newer = await ready_import(env, chunk_count=2)
    await make_index(env, newer, status="building", queryable=False)
    base = f"/api/v1/repositories/{env.repository.id}/knowledge"
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        app.state.knowledge_index = ProbeIndex("collection_missing")
        response = await client.get(base)
        assert response.status_code == 200
        assert response.json()["index_status"] == "building"
        assert response.json()["index_reason"] == "target_building"
        assert app.state.knowledge_index.calls == []


async def test_overview_rejects_out_of_range_page_sizes(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        base = f"/api/v1/repositories/{env.repository.id}/knowledge"
        assert (await client.get(f"{base}?limit=0")).status_code == 422
        assert (await client.get(f"{base}?limit=101")).status_code == 422
        assert (await client.get(f"{base}?limit=100")).status_code == 200
        assert (await client.get(f"{base}?cursor=forged")).status_code == 400


async def test_write_routes_require_csrf_and_admin(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        path = f"/api/v1/repositories/{env.repository.id}/knowledge/imports"
        body = {
            "kind": "delete",
            "repository_id": str(env.repository.id),
            "document_id": str(uuid4()),
        }
        assert (await client.post(path, json=body)).status_code == 403
        signed = await headers(env, client, key="delete-1")
        assert (await client.post(path, json=body, headers=signed)).status_code == 404

        async with env.factory.begin() as db:
            user = User(github_id=4242, role="member")
            db.add(user)
            await db.flush()
            member_id, member_version = user.id, user.auth_version
        member = await session_for(
            env, type("Row", (), {"id": member_id, "auth_version": member_version})()
        )
        client.cookies.set("devflow_session", member)
        current = (await client.get("/api/v1/session")).json()
        response = await client.post(
            path,
            json=body,
            headers={
                "Origin": env.settings.public_base_url,
                "X-CSRF-Token": current["csrf_token"],
                "Idempotency-Key": "delete-2",
            },
        )
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"


async def test_staging_rejects_oversized_stream_without_persisting(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        response = await stage(env, client, body=b"x" * 1048577, key="too-big")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
        assert await count(env, UploadStage) == 0
        assert await count(env, Document) == 0


async def test_staging_ignores_content_length_and_accepts_real_small_body(
    knowledge_env,
):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        value = await headers(env, client, key="liar")
        value["Content-Length"] = str(64 * 1024 * 1024)
        response = await client.post(
            f"/api/v1/repositories/{env.repository.id}/knowledge/uploads"
            "?filename=note.md",
            content=b"# small\n",
            headers=value,
        )
        assert response.status_code == 201
        assert set(response.json()) == {"staging_id", "expires_at"}


async def test_staging_replay_reuses_one_stage_and_conflicts_on_new_bytes(
    knowledge_env,
):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        first = await stage(env, client, key="same")
        assert first.status_code == 201
        replay = await stage(env, client, key="same")
        assert replay.status_code == 201
        assert replay.json()["staging_id"] == first.json()["staging_id"]
        assert replay.json()["expires_at"] == first.json()["expires_at"]
        assert await count(env, UploadStage) == 1
        assert await count(env, Document) == 1

        changed = await stage(env, client, body=b"# different\n", key="same")
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "idempotency_conflict"
        assert await count(env, UploadStage) == 1


async def test_staging_enforces_upload_filename_and_utf8_rules(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        for filename in ("note.rst", "note.py", "note.pdf", "notes.zip", ".env.md"):
            response = await stage(
                env, client, filename=filename, key=f"name-{filename}"
            )
            assert response.status_code == 422, filename
        invalid = await stage(env, client, body=b"\xff\xfe", key="utf8")
        assert invalid.status_code == 422
        assert await count(env, UploadStage) == 0


async def test_upload_import_replays_admission_without_restaging(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        staged = await stage(env, client, key="stage-replay")
        staging_id = staged.json()["staging_id"]
        body = {
            "kind": "upload",
            "repository_id": str(env.repository.id),
            "filename": "note.md",
            "staging_id": staging_id,
        }
        path = f"/api/v1/repositories/{env.repository.id}/knowledge/imports"
        first = await client.post(
            path, json=body, headers=await headers(env, client, key="admit-1")
        )
        assert first.status_code == 202
        receipt = first.json()
        assert set(receipt) == {"import_id", "job_id", "target_generation"}
        assert receipt["target_generation"] == 1
        assert first.headers["cache-control"] == "private, no-store"

        # Response loss: the same admission key returns the original receipt.
        replay = await client.post(
            path, json=body, headers=await headers(env, client, key="admit-1")
        )
        assert replay.status_code == 202
        assert replay.json() == receipt
        assert await count(env, KnowledgeImport) == 1
        assert await count(env, UploadStage) == 1

        conflict = await client.post(
            path,
            json={**body, "filename": "other.md"},
            headers=await headers(env, client, key="admit-1"),
        )
        assert conflict.status_code == 409

        async with env.factory() as db:
            imported = await db.scalar(select(KnowledgeImport))
            assert imported.kind == "upload"
            assert imported.composition_version == 1
            assert imported.request["filename"] == "note.md"


async def test_replacement_and_deletion_return_bounded_receipts(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        staged = await stage(env, client, key="base-stage")
        body = {
            "kind": "upload",
            "repository_id": str(env.repository.id),
            "filename": "note.md",
            "staging_id": staged.json()["staging_id"],
        }
        await client.post(
            f"/api/v1/repositories/{env.repository.id}/knowledge/imports",
            json=body,
            headers=await headers(env, client, key="base-admit"),
        )
        async with env.factory() as db:
            document = await db.scalar(select(Document))

        replacement_stage = await stage(
            env,
            client,
            body=b"# replaced\n",
            key="replacement-stage",
            document_id=document.id,
        )
        assert replacement_stage.status_code == 201
        replacement = await client.post(
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document.id}/replacements",
            json={
                "filename": "note.md",
                "staging_id": replacement_stage.json()["staging_id"],
            },
            headers=await headers(env, client, key="replacement-admit"),
        )
        assert replacement.status_code == 202
        assert replacement.json()["target_generation"] == 2

        deletion = await client.post(
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document.id}/deletions",
            headers=await headers(env, client, key="deletion-admit"),
        )
        assert deletion.status_code == 202
        assert deletion.json()["target_generation"] == 3

        repeated = await client.post(
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document.id}/deletions",
            headers=await headers(env, client, key="deletion-admit-2"),
        )
        assert repeated.status_code == 404

        async with env.factory() as db:
            refreshed = await db.get(Document, document.id)
            assert refreshed.deleted is True
            assert refreshed.accepted_revision_id is None


async def test_document_detail_hides_staged_revisions(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        staged = await stage(env, client, key="detail-stage")
        async with env.factory() as db:
            document = await db.scalar(select(Document))
        response = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document.id}"
        )
        # The document exists but nothing is published, so it exposes no revision.
        assert response.status_code == 200
        view = response.json()
        assert view["revisions"] == []
        assert view["preview"] is None
        assert view["deleted"] is False
        assert staged.status_code == 201

        # A document with no published revision is private to the ingest flow;
        # the list projection must not reveal its existence yet.
        listing = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge"
        )
        assert listing.json()["documents"] == {"items": [], "next_cursor": None}


async def test_revision_history_is_keyset_paginated_and_addressable(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        document_id, revision_ids = await published_upload(
            env,
            [("available", f"# body {index}\n".encode()) for index in range(35)],
        )
        base = (
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document_id}"
        )

        first = await client.get(base)
        assert first.status_code == 200
        view = first.json()
        # Default page is 30; the cursor is the only way to reach older history.
        assert [item["revision_id"] for item in view["revisions"]] == [
            str(value) for value in reversed(revision_ids[5:])
        ]
        assert view["revisions"][0]["revision_id"] == str(revision_ids[-1])
        assert view["revisions_next_cursor"]
        assert all(item["availability"] == "available" for item in view["revisions"])

        second = await client.get(
            base, params={"revision_cursor": view["revisions_next_cursor"]}
        )
        assert second.status_code == 200
        rest = second.json()
        assert [item["revision_id"] for item in rest["revisions"]] == [
            str(value) for value in reversed(revision_ids[:5])
        ]
        assert rest["revisions_next_cursor"] is None

        # An explicitly named revision is readable even though it is not on the
        # default page; pagination must not hide known published history.
        oldest = await client.get(base, params={"revision_id": str(revision_ids[0])})
        assert oldest.status_code == 200
        assert oldest.json()["preview"]["text"] == "# body 0\n"
        assert len(oldest.json()["revisions"]) == 30

        forged = await client.get(
            base,
            params={"revision_cursor": view["revisions_next_cursor"][:-4] + "AAAA"},
        )
        assert forged.status_code == 400
        assert forged.json()["error"]["code"] == "invalid_cursor"

        beyond = await client.get(base, params={"revision_limit": 101})
        assert beyond.status_code == 422

        overview = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge"
        )
        assert overview.json()["documents"]["items"][0]["published_revisions"] == 35


async def test_reclaimed_revision_keeps_identity_and_reports_expiry(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        document_id, (expired_id, available_id) = await published_upload(
            env, [("expired", None), ("available", b"# fresh\n")]
        )
        base = (
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/"
            f"{document_id}"
        )

        view = (await client.get(base)).json()
        listed = {item["revision_id"]: item for item in view["revisions"]}
        # Reclaimed history is not erased: the revision keeps its identity and
        # says why its body is missing instead of disappearing from the list.
        assert listed[str(expired_id)]["availability"] == "expired"
        assert listed[str(available_id)]["availability"] == "available"
        assert view["preview"]["text"] == "# fresh\n"

        expired = await client.get(base, params={"revision_id": str(expired_id)})
        assert expired.status_code == 200
        preview = expired.json()["preview"]
        assert preview["reason"] == "source_expired"
        assert preview["text"] is None

        overview = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge"
        )
        assert overview.json()["documents"]["items"][0]["published_revisions"] == 2


async def test_fork_source_metadata_requires_current_source_authorization(
    knowledge_env,
):
    env = knowledge_env
    fork = await env.fork()
    version = await env.workspace(fork)
    body = b"# guide\n"
    digest = hashlib.sha256(body).hexdigest()
    path = "docs/guide.md"
    async with env.factory.begin() as db:
        document = Document(
            repository_id=env.repository.id,
            source_repository_id=fork.id,
            kind="workspace",
            path=path,
        )
        db.add(document)
        await db.flush()
        revision = DocumentRevision(
            document_id=document.id,
            revision_hash=digest,
            source_version=workspace_source_version(
                repository_id=env.repository.id,
                source_repository_id=fork.id,
                dependency_repository_ids=[env.repository.id, fork.id],
                workspace_id=version.id,
                sha=version.commit_sha,
                path=path,
            ),
            workspace_id=version.id,
            sha=version.commit_sha,
            body=body,
            body_bytes=len(body),
            published_at=await db.scalar(select(func.clock_timestamp())),
        )
        db.add(revision)
        await db.flush()
        db.add_all(
            [
                RevisionDependency(revision_id=revision.id, repository_id=owner)
                for owner in (env.repository.id, fork.id)
            ]
        )
        document_id = document.id

    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        base = f"/api/v1/repositories/{env.repository.id}/knowledge"
        # The document lives in this repository, but its bytes belong to a fork
        # the current selection does not authorize; metadata must not leak it.
        hidden = await client.get(f"{base}/documents/{document_id}")
        assert hidden.status_code == 404
        listing = await client.get(base)
        assert listing.json()["documents"]["items"] == []

        # Selecting the fork-backed Workspace is what restores visibility.
        async with env.factory.begin() as db:
            db.add(
                KnowledgeCorpus(
                    repository_id=env.repository.id,
                    target_workspace_id=version.id,
                )
            )
        visible = await client.get(f"{base}/documents/{document_id}")
        assert visible.status_code == 200
        preview = visible.json()["preview"]
        assert preview["reason"] is None
        assert preview["text"] == "# guide\n"
        listing = await client.get(base)
        assert [
            item["document_id"] for item in listing.json()["documents"]["items"]
        ] == [str(document_id)]

        missing = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge/documents/{uuid4()}"
        )
        assert missing.status_code == 404


async def test_paths_rejects_unbounded_scopes_before_any_git_read(knowledge_env):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        base = f"/api/v1/repositories/{env.repository.id}/knowledge/paths"
        sha = "a" * 40
        workspace_id = uuid4()
        too_many = "&".join(f"document_paths=docs{p}" for p in range(129))
        response = await client.get(
            f"{base}?workspace_id={workspace_id}&sha={sha}&{too_many}"
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        assert (
            await client.get(f"{base}?workspace_id={workspace_id}")
        ).status_code == 422
        assert (
            await client.get(f"{base}?workspace_id={workspace_id}&sha=not-a-sha")
        ).status_code == 422
        assert (
            await client.get(f"{base}?workspace_id={workspace_id}&sha={sha}&limit=101")
        ).status_code == 422
        unknown = await client.get(f"{base}?workspace_id={workspace_id}&sha={sha}")
        assert unknown.status_code == 404


@pytest.mark.parametrize("limit", [30, 100])
async def test_document_page_bounds_are_accepted(knowledge_env, limit):
    env = knowledge_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge?limit={limit}"
        )
        assert response.status_code == 200


class PathCache:
    """Only Git transport is replaced; the guarded reader stays real."""

    def __init__(self, root):
        from test_workspace_reads import MemoryCache

        self._inner = MemoryCache(root)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def blob_size(self, repo, oid):
        return len(self._inner.blobs[oid])


async def test_paths_page_reports_real_policy_outcomes_and_continuation(
    knowledge_env, workspace_tmp_path
):
    env = knowledge_env
    version = await env.workspace()
    async with env.factory.begin() as db:
        db.add(
            Document(
                repository_id=env.repository.id,
                source_repository_id=version.source_repository_id,
                kind="workspace",
                path="docs/gone.md",
                deleted=True,
            )
        )
    async with browser(env) as (client, app, raw):
        cache = PathCache(workspace_tmp_path)
        for name in (
            b"README.md",
            b"docs/a.md",
            b"docs/b.pdf",
            b"docs/gone.md",
            b"src/app.py",
            b".env",
        ):
            cache.add(name, b"body")
        app.state.workspace_cache = cache
        client.cookies.set("devflow_session", raw)
        base = f"/api/v1/repositories/{env.repository.id}/knowledge/paths"
        response = await client.get(
            f"{base}?workspace_id={version.id}&sha={version.commit_sha}&limit=100"
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        page = response.json()
        outcomes = {
            item["path"]: (item["outcome"], item["reason"]) for item in page["items"]
        }
        assert outcomes["README.md"] == ("selected", None)
        assert outcomes["docs/a.md"] == ("selected", None)
        assert outcomes["docs/b.pdf"] == ("unsupported", "unsupported")
        assert outcomes["docs/gone.md"] == ("excluded", "source_deleted")
        assert outcomes["src/app.py"] == ("excluded", "not_selected")
        assert outcomes[".env"] == ("excluded", "excluded_hidden")
        assert all(item["size"] == 4 for item in page["items"])
        assert page["exhausted"] is True
        assert page["truncated"] is False
        assert page["next_cursor"] is None

        first = await client.get(
            f"{base}?workspace_id={version.id}&sha={version.commit_sha}&limit=2"
        )
        assert first.status_code == 200
        assert first.json()["next_cursor"] is not None
        assert first.json()["exhausted"] is False
        second = await client.get(
            f"{base}?workspace_id={version.id}&sha={version.commit_sha}"
            f"&limit=2&cursor={first.json()['next_cursor']}"
        )
        assert second.status_code == 200
        # Raw-byte order puts ".env" and "README.md" on the first page.
        assert second.json()["items"][0]["path"] == "docs/a.md"
        # The cursor is bound to its page size and to this actor/repository.
        assert (
            await client.get(
                f"{base}?workspace_id={version.id}&sha={version.commit_sha}"
                f"&limit=3&cursor={first.json()['next_cursor']}"
            )
        ).status_code == 400
        assert (
            await client.get(
                f"{base}?workspace_id={version.id}&sha={version.commit_sha}"
                f"&limit=2&cursor=forged"
            )
        ).status_code == 400


async def test_paths_rejects_empty_scope_selection(knowledge_env):
    env = knowledge_env
    version = await env.workspace()
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get(
            f"/api/v1/repositories/{env.repository.id}/knowledge/paths"
            f"?workspace_id={version.id}&sha={version.commit_sha}&document_paths_explicit=true"
        )
        assert response.status_code == 422
