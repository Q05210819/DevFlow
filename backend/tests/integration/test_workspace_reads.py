"""Reader behavior over real authorization/DB admission; bounded Git fixtures."""

import asyncio
import hashlib
import importlib
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from support.repository_env import browser
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from test_git_objects import objects as objects  # noqa: PLC0414
from test_workspace_prepare import (
    claim,
    fork,
    request,
)
from test_workspace_prepare import (
    prepare_env as prepare_env,  # noqa: PLC0414
)
from test_workspace_prepare import (
    workspace_env as workspace_env,  # noqa: PLC0414
)

from devflow.errors import AppError
from devflow.workspace.git import TreeEntries, TreeEntry
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion
from devflow.workspace.process import WorkspaceProcessError


@pytest.fixture
def tmp_path(workspace_tmp_path):
    return workspace_tmp_path


class MemoryCache:
    """Only raw Git I/O is replaced; no fake claim of physical process proof."""

    def __init__(self, root):
        self.root = root
        self.entries = []
        self.blobs = {}
        self.truncated = False
        self.calls = []
        self.after = None
        self.entered = threading.Event()
        self.release = None
        self.closed = False
        self.cancelled = False

    def add(self, path, body=b"hello\n", mode="100644"):
        oid = hashlib.sha1(body).hexdigest()
        self.entries.append(TreeEntry(mode, oid, path))
        self.blobs[oid] = body
        return oid

    def lock_path(self, repo):
        return self.root / ".locks" / f"{repo}.lock"

    @contextmanager
    def operation(self, guard, *, deadline, cancel):
        self.thread = threading.get_ident()
        self.deadline = deadline
        self.entered.set()
        try:
            while self.release is not None and not self.release.is_set():
                if cancel.wait(0.005):
                    self.cancelled = True
                    raise WorkspaceProcessError("cancelled")
            yield
        finally:
            self.closed = True
            if self.after:
                self.after()

    def tree(self, repo, sha):
        assert threading.get_ident() == self.thread
        self.calls.append(("tree", repo, sha))
        return TreeEntries(
            tuple(self.entries),
            not self.truncated,
            self.truncated,
            len(self.entries),
            "tree_limit" if self.truncated else None,
        )

    def blob(self, repo, oid, max_bytes):
        assert threading.get_ident() == self.thread and not self.truncated
        self.calls.append(("blob", repo, oid, max_bytes))
        body = self.blobs[oid]
        if len(body) > max_bytes:
            raise WorkspaceProcessError("blob_too_large")
        return body


def reader(env):
    try:
        mod = importlib.import_module("devflow.workspace.read")
    except ModuleNotFoundError:
        pytest.fail("authorized Workspace reader is not implemented")
    return mod.WorkspaceReader(env.factory, env.policy, env.cache)


@pytest_asyncio.fixture
async def read_env(workspace_env):
    env = workspace_env
    env.view = await request(env)
    async with env.factory.begin() as db:
        version = await db.get(WorkspaceVersion, env.view.id)
        version.status = "ready"
        version.pin_ref = "refs/devflow/pins/" + uuid4().hex
    env.cache = MemoryCache(env.root)
    return env


def args(env):
    return env.actor, env.repository.id, env.view.id


async def drained(env):
    async with env.factory() as db:
        records = list(await db.scalars(select(WorkspaceProcessGuard)))
    assert records and all(item.state == "drained" for item in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["../x", "/x", "a//b", "./x", "a/../b", "a/", "a\\b", "a\0", "\ud800", "x" * 4097],
)
async def test_invalid_path_never_reads_git(read_env, path):
    env = read_env
    with pytest.raises(AppError) as caught:
        await reader(env).file(*args(env), path=path)
    assert caught.value.code == "validation_error"
    assert env.cache.calls == []


@pytest.mark.asyncio
async def test_exact_paths_raw_names_and_tree_cursor_scope(read_env):
    env = read_env
    for name in [b"A.txt", b"a.txt", b"dir/a", b"dir/b", b"C:/x", b"\xff"]:
        env.cache.add(name)
    service = reader(env)
    page = await service.tree(*args(env), limit=2)
    assert [item.path for item in page.items] == ["A.txt", "a.txt"]
    assert page.has_more and page.complete and not page.truncated
    second = await service.tree(*args(env), limit=2, cursor=page.next_cursor)
    assert [item.path for item in second.items] == ["dir", "C:"]
    assert all(item.oid is None for item in second.items)
    last = await service.tree(*args(env), limit=2, cursor=second.next_cursor)
    assert last.items[0].path is None
    assert last.items[0].reason == "unsupported_path"
    assert last.items[0].source_url is None and not last.has_more
    with pytest.raises(AppError) as caught:
        await service.tree(*args(env), path="dir", limit=2, cursor=page.next_cursor)
    assert caught.value.code == "invalid_cursor"
    assert (await service.file(*args(env), path="C:/x")).text == "hello\n"
    with pytest.raises(AppError) as caught:
        await service.file(*args(env), path="a.TXT")
    assert caught.value.code == "workspace_not_found"
    await drained(env)


@pytest.mark.asyncio
async def test_lf_exact_ranges_response_bytes_and_safe_citations(read_env):
    env = read_env
    name = "a %?#é.txt"
    oid = env.cache.add(name.encode(), b"a\r\nb\rc\nlast")
    result = await reader(env).file(*args(env), path=name, start_line=2, line_count=1)
    assert result.text == "b\rc\n"
    assert (result.start_line, result.end_line, result.total_lines) == (2, 2, 3)
    assert result.blob_oid == oid and result.next_line == 3
    assert result.source_url.endswith("/a%20%25%3F%23%C3%A9.txt#L2")
    assert env.view.commit_sha in result.source_url
    env.cache.add(b"long", b"x" * (256 * 1024 + 1))
    long = await reader(env).file(*args(env), path="long")
    assert long.text == "" and long.start_line is None and long.end_line is None
    assert (
        long.truncated and long.reason == "response_limit" and long.source_url is None
    )
    env.cache.add(b"boundary", ("é" * (128 * 1024)).encode())
    boundary = await reader(env).file(*args(env), path="boundary")
    assert boundary.complete and len(boundary.text.encode()) == 256 * 1024
    env.cache.add(b"big", b"x" * (1024 * 1024 + 1))
    big = await reader(env).file(*args(env), path="big", start_line=500, line_count=1)
    assert big.kind == "too_large" and not big.text and big.start_line is None
    await drained(env)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,mode,kind",
    [
        (b"", "100644", "text"),
        (b"\0", "100644", "binary"),
        (b"\xff", "100644", "non_utf8"),
        (b"../../secret", "120000", "symlink"),
        (b"", "160000", "gitlink"),
        (
            b"version https://git-lfs.github.com/spec/v1\noid sha256:"
            + b"a" * 64
            + b"\nsize 1\n",
            "100644",
            "lfs_pointer",
        ),
    ],
)
async def test_file_types_never_dereference_special_objects(read_env, body, mode, kind):
    env = read_env
    env.cache.add(b"file", body, mode)
    result = await reader(env).file(*args(env), path="file")
    assert result.kind == kind
    if kind in {"symlink", "gitlink"}:
        assert not any(call[0] == "blob" for call in env.cache.calls)
    if kind == "gitlink":
        assert result.blob_oid is None
    if not body:
        assert result.start_line is None and result.end_line is None
    await drained(env)


@pytest.mark.asyncio
async def test_search_literal_matching_lines_and_separate_snippet_coverage(read_env):
    env = read_env
    env.cache.add(b"a", b"x.* x.*\r\nx\n" + b"x.*" + b"z" * (256 * 1024) + b"\n")
    env.cache.add(b"binary", b"x.*\0")
    result = await reader(env).search(*args(env), query="x.*")
    assert len(result.items) == 2 and result.complete and not result.truncated
    assert result.items[0].text == "x.* x.*\r\n"
    assert result.items[1].text is None and result.items[1].reason == "response_limit"
    assert result.items[1].citation.start_line == 3
    assert result.snippets_truncated
    assert result.scanned.text_files == 1 and result.skipped["binary"] == 1
    assert (await reader(env).search(*args(env), query="\r")).items[
        0
    ].citation.start_line == 1
    with pytest.raises(AppError):
        await reader(env).search(*args(env), query="x\nx")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["matches", "files", "bytes", "tree"])
async def test_search_caps_never_claim_unseen_scope_complete(read_env, cap):
    env = read_env
    if cap == "matches":
        env.cache.add(b"a", b"hit\n" * 101)
        query, reason = "hit", "match_limit"
    elif cap == "files":
        for number in range(2001):
            env.cache.add(str(number).encode(), b"no")
        query, reason = "missing", "file_limit"
    elif cap == "bytes":
        for number in range(21):
            env.cache.add(str(number).encode(), b"n" * (1024 * 1024))
        query, reason = "missing", "byte_limit"
    else:
        env.cache.add(b"a")
        env.cache.truncated = True
        query, reason = "missing", "tree_limit"
    result = await reader(env).search(*args(env), query=query)
    assert result.truncated and not result.complete and result.reason == reason
    assert result.scanned.text_files <= 2000
    assert result.scanned.blob_bytes <= 20 * 1024 * 1024
    assert len(result.items) <= 100
    if cap == "tree":
        assert not any(call[0] == "blob" for call in env.cache.calls)


@pytest.mark.asyncio
async def test_authorization_fence_after_materialization_discards_content(
    read_env, monkeypatch
):
    from devflow.repositories.models import Repository

    env = read_env
    env.cache.add(b"file")
    original = env.policy.local

    async def local(actor, repo):
        if env.cache.closed:
            async with env.factory.begin() as db:
                row = await db.get(Repository, repo)
                row.auth_version += 1
        return await original(actor, repo)

    monkeypatch.setattr(env.policy, "local", local)
    with pytest.raises(AppError) as caught:
        await reader(env).file(*args(env), path="file")
    assert caught.value.code == "workspace_not_found"
    await drained(env)


@pytest.mark.asyncio
async def test_cancel_joins_owned_thread_and_revokes_only_unactivated_guard(read_env):
    env = read_env
    env.cache.release = threading.Event()
    pending = asyncio.create_task(reader(env).tree(*args(env)))
    assert await asyncio.to_thread(env.cache.entered.wait, 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert env.cache.cancelled and env.cache.closed
    await drained(env)


@pytest.mark.asyncio
async def test_routes_metadata_content_limits_and_no_store(read_env):
    env = read_env
    env.cache.add(b"file")
    base = f"/api/v1/repositories/{env.repository.id}/workspaces"
    async with browser(env) as (client, app, raw):
        app.state.workspace_cache = env.cache
        client.cookies.set("devflow_session", raw)
        listed = await client.get(base)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == str(env.view.id)
        result = await client.get(
            base + f"/{env.view.id}/file", params={"path": "file"}
        )
        assert result.status_code == 200 and result.json()["text"] == "hello\n"
        assert result.headers["cache-control"] == "no-store"
        assert (await client.get(base, params={"limit": 101})).status_code == 422
        tree = base + f"/{env.view.id}/tree"
        assert (await client.get(tree, params={"limit": 200})).status_code == 200
        assert (await client.get(tree, params={"limit": 201})).status_code == 422
        invalid = await client.get(tree + "?path=%FF")
        assert invalid.status_code == 422
        forbidden = await client.post(base, json={"kind": "branch", "name": "main"})
        assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_real_git_reads_use_production_database_guard_and_drain(prepare_env):
    env = prepare_env
    env.view = await request(env)
    await env.workspace.prepare(await claim(env))
    env.cache = env.workspace.cache
    service = reader(env)
    result = await service.file(*args(env), path="text.txt")
    assert result.text == "line1\r\nline2\n" and result.end_line == 2
    page = await service.tree(*args(env))
    assert {item.kind for item in page.items} == {"file", "symlink", "gitlink"}
    async with env.factory() as db:
        guards = list(
            await db.scalars(
                select(WorkspaceProcessGuard).where(
                    WorkspaceProcessGuard.kind == "read"
                )
            )
        )
    assert len(guards) == 2
    assert all(row.state == "drained" and row.proof == "physical" for row in guards)


@pytest.mark.asyncio
async def test_shared_fork_reader_uses_current_member_and_rechecks_source(
    read_env, monkeypatch
):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.models import GitHubCredential
    from devflow.repositories.models import Repository

    env = read_env
    foreign = await fork(env)
    env.view = await request(
        env, "fork-head", {"kind": "pull", "number": 7, "side": "head"}
    )
    async with env.factory.begin() as db:
        (await db.get(WorkspaceVersion, env.view.id)).status = "ready"
        creator = await db.get(User, env.actor.id)
        creator.enabled = False
        member = User(github_id=77, role="member")
        db.add(member)
        await db.flush()
        db.add(
            GitHubCredential(
                user_id=member.id,
                access_token_encrypted=env.cipher.encrypt(SecretStr("member")),
            )
        )
        env.actor = Actor(member.id, 77, "member", member.auth_version)
    env.cache.add(b"file")
    result = await reader(env).file(*args(env), path="file")
    assert result.source_repository_id == foreign.id
    assert "/team/fork/blob/" in result.source_url
    original = env.policy.local

    async def revoke_on_final_check(actor, identity):
        if env.cache.closed and identity == foreign.id:
            async with env.factory.begin() as db:
                source = await db.get(Repository, identity)
                source.auth_version += 1
        return await original(actor, identity)

    env.cache.closed = False
    monkeypatch.setattr(env.policy, "local", revoke_on_final_check)
    with pytest.raises(AppError) as caught:
        await reader(env).file(*args(env), path="file")
    assert caught.value.code == "fork_source_unavailable"
    await drained(env)


@pytest.mark.asyncio
async def test_base_first_missing_snapshot_and_global_admission(read_env):
    from devflow.workspace.guards import reserve_guard, revoke_reservation

    env = read_env
    service = reader(env)
    with pytest.raises(AppError) as caught:
        await service.tree(env.actor, uuid4(), env.view.id)
    assert caught.value.code == "resource_not_found"
    with pytest.raises(AppError) as caught:
        await service.tree(env.actor, env.repository.id, uuid4())
    assert caught.value.code == "workspace_not_found"
    guards = [
        await reserve_guard(
            env.factory,
            env.settings,
            env.view.id,
            kind="read",
            actor=env.actor,
            deadline=time.monotonic() + 5,
            lock_path=env.cache.lock_path(101),
        )
        for _ in range(4)
    ]
    with pytest.raises(AppError) as caught:
        await service.tree(*args(env))
    assert caught.value.code == "workspace_busy" and env.cache.calls == []
    for guard in guards:
        await revoke_reservation(env.factory, guard)
    assert (await service.tree(*args(env))).complete


@pytest.mark.asyncio
async def test_five_second_deadline_includes_provider_authorization(
    read_env, monkeypatch
):
    env = read_env
    original = env.policy.require

    async def slow(*args, **kwargs):
        await asyncio.sleep(6)
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.policy, "require", slow)
    started = time.monotonic()
    with pytest.raises(AppError) as caught:
        await reader(env).tree(*args(env))
    assert caught.value.code == "attempt_timeout"
    assert time.monotonic() - started < 5.5 and env.cache.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["timeout", "caller_cancel"])
async def test_revocation_reclassification_keeps_deadline_and_cancellation(
    read_env, monkeypatch, exit_kind
):
    env = read_env
    service = reader(env)
    reclassifying = asyncio.Event()
    calls = 0

    async def revoked_then_slow_fence(actor, version):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AppError("permission_revoked", 404, "Workspace scope unavailable")
        reclassifying.set()
        await asyncio.sleep(6)

    monkeypatch.setattr(service, "_fence", revoked_then_slow_fence)
    started = time.monotonic()
    pending = asyncio.create_task(service.tree(*args(env)))
    await asyncio.wait_for(reclassifying.wait(), timeout=3)
    if exit_kind == "caller_cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(AppError) as caught:
            await pending
        assert caught.value.code == "attempt_timeout" and caught.value.status == 503
        assert 4.5 <= time.monotonic() - started < 5.5
    assert env.cache.closed and calls == 2
    await drained(env)


@pytest.mark.asyncio
async def test_authorization_time_is_not_reset_at_git_entry(read_env, monkeypatch):
    env = read_env
    original = env.policy.require

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.policy, "require", slow)
    started = time.monotonic()
    await reader(env).tree(*args(env))
    assert env.cache.deadline <= started + 5.05
    assert env.cache.deadline - time.monotonic() < 4.9


@pytest.mark.asyncio
async def test_long_utf8_path_cursor_and_no_normalization(read_env):
    env = read_env
    directory = "é" * 2046
    env.cache.add((directory + "/a").encode())
    env.cache.add((directory + "/b").encode())
    env.cache.add(b"e\xcc\x81", b"decomposed")
    env.cache.add("é".encode(), b"composed")
    service = reader(env)
    first = await service.tree(*args(env), path=directory, limit=1)
    assert len(first.next_cursor) < 8192
    last = await service.tree(
        *args(env), path=directory, limit=1, cursor=first.next_cursor
    )
    assert last.items[0].path == directory + "/b"
    assert (await service.file(*args(env), path="é")).text == "composed"
    assert (await service.file(*args(env), path="e\u0301")).text == "decomposed"
    maximum = "é" * 2048
    env.cache.add(maximum.encode(), b"boundary")
    assert (await service.file(*args(env), path=maximum)).text == "boundary"


@pytest.mark.asyncio
async def test_tree_scan_cap_preserves_prefix_without_blob_reads(read_env):
    env = read_env
    env.cache.add(b"file")
    env.cache.truncated = True
    service = reader(env)
    page = await service.tree(*args(env))
    assert (
        page.items[0].path == "file" and page.truncated and page.reason == "tree_limit"
    )
    result = await service.file(*args(env), path="file")
    assert (
        result.kind == "unavailable"
        and result.reason == "tree_limit"
        and not result.text
    )
    assert not any(call[0] == "blob" for call in env.cache.calls)


@pytest.mark.asyncio
async def test_file_line_count_limit_and_empty_or_outside_ranges(read_env):
    env = read_env
    service = reader(env)
    env.cache.add(b"lines", b"\n" * 501)
    result = await service.file(*args(env), path="lines")
    assert result.end_line == 500 and result.next_line == 501
    assert result.total_lines == 501 and result.truncated
    tail = await service.file(*args(env), path="lines", start_line=501)
    assert tail.start_line == tail.end_line == 501 and tail.next_line is None
    outside = await service.file(*args(env), path="lines", start_line=502)
    assert outside.start_line is None and outside.text == ""
    for count in (0, 501, True):
        with pytest.raises(AppError) as caught:
            await service.file(*args(env), path="lines", line_count=count)
        assert caught.value.code == "validation_error"
    env.cache.add(b"exact", b"x" * (1024 * 1024))
    exact = await service.file(*args(env), path="exact")
    assert exact.kind == "text" and exact.reason == "response_limit"


@pytest.mark.asyncio
async def test_query_byte_bounds_and_exact_cap_completion(read_env):
    env = read_env
    env.cache.add(b"a", ("é" * 128 + "\n").encode())
    service = reader(env)
    assert len((await service.search(*args(env), query="é" * 128)).items) == 1
    for query in ("", "x" * 257, "é" * 129, "\ud800"):
        with pytest.raises(AppError) as caught:
            await service.search(*args(env), query=query)
        assert caught.value.code == "validation_error"
    env.cache.entries.clear()
    env.cache.add(b"a", b"yes\n" * 100)
    exact = await service.search(*args(env), query="yes")
    assert len(exact.items) == 100 and exact.complete
    assert exact.scanned.blob_bytes == 400


@pytest.mark.asyncio
async def test_remaining_byte_budget_never_reads_oversized_next_blob(read_env):
    env = read_env
    for number in range(19):
        env.cache.add(str(number).encode(), b"\0" * (1024 * 1024))
    env.cache.add(b"small", b"\0" * (1024 * 1024 - 10))
    env.cache.add(b"next", b"x" * 11)
    result = await reader(env).search(*args(env), query="missing")
    assert result.reason == "byte_limit"
    assert result.scanned.blob_bytes == 20 * 1024 * 1024 - 10
    assert result.scanned.text_files == 0 and result.skipped["binary"] == 20
    assert env.cache.calls[-1][-1] == 10


@pytest.mark.asyncio
async def test_read_error_cannot_drain_uncertain_active_guard(read_env):
    env = read_env
    entered = threading.Event()
    release = threading.Event()
    env.cache.release = release
    pending = asyncio.create_task(reader(env).tree(*args(env)))
    assert await asyncio.to_thread(env.cache.entered.wait, 3)
    async with env.factory.begin() as db:
        guard = await db.scalar(select(WorkspaceProcessGuard))
        # Model an uncertain active physical operation. The API may never
        # rewrite it to never_activated merely because its caller cancels.
        guard.state = "active"
        entered.set()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with env.factory() as db:
        guard = await db.scalar(select(WorkspaceProcessGuard))
        assert guard.state == "active" and guard.proof is None


@pytest.mark.asyncio
async def test_metadata_post_replay_and_error_responses_never_cache(read_env):
    env = read_env
    base = f"/api/v1/repositories/{env.repository.id}/workspaces"
    async with browser(env) as (client, app, raw):
        app.state.workspace_cache = env.cache
        client.cookies.set("devflow_session", raw)
        session = await client.get("/api/v1/session")
        assert session.status_code == 200
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": session.json()["csrf_token"],
            "Idempotency-Key": "first",
        }
        # Replay uses stored scopes, never the route's real metadata client.
        replay = await client.post(
            base, json={"kind": "branch", "name": "main"}, headers=headers
        )
        assert replay.status_code == 202 and replay.json()["id"] == str(env.view.id)
        missing = await client.get(base + f"/{uuid4()}/tree")
        assert missing.json()["error"]["code"] == "workspace_not_found"
        assert missing.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_native_reader_cancel_joins_supervisor_and_acknowledges_physical_drain(
    prepare_env, monkeypatch
):
    env = prepare_env
    env.view = await request(env)
    await env.workspace.prepare(await claim(env))
    env.cache = env.workspace.cache
    original = env.cache.runner.run
    entered = threading.Event()

    def pause_before_tree(args, **kwargs):
        if args[0] == "ls-tree":
            entered.set()
            assert kwargs["cancel"].wait(4)
        return original(args, **kwargs)

    monkeypatch.setattr(env.cache.runner, "run", pause_before_tree)
    pending = asyncio.create_task(reader(env).tree(*args(env)))
    assert await asyncio.to_thread(entered.wait, 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with env.factory() as db:
        guard = await db.scalar(
            select(WorkspaceProcessGuard).where(WorkspaceProcessGuard.kind == "read")
        )
        assert guard.state == "drained" and guard.proof == "physical"


@pytest.mark.asyncio
async def test_final_fork_fence_cannot_miss_base_change_during_source_check(
    read_env, monkeypatch
):
    from devflow.repositories.models import Repository

    env = read_env
    foreign = await fork(env)
    env.view = await request(
        env, "fork-race", {"kind": "pull", "number": 7, "side": "head"}
    )
    async with env.factory.begin() as db:
        (await db.get(WorkspaceVersion, env.view.id)).status = "ready"
    env.cache.add(b"file")
    original = env.policy.local
    source_checks = 0

    async def race(actor, identity):
        nonlocal source_checks
        if env.cache.closed and identity == foreign.id:
            source_checks += 1
            if source_checks == 2:
                async with env.factory.begin() as db:
                    base = await db.get(Repository, env.repository.id)
                    base.auth_version += 1
        return await original(actor, identity)

    monkeypatch.setattr(env.policy, "local", race)
    with pytest.raises(AppError) as caught:
        await reader(env).file(*args(env), path="file")
    assert caught.value.code == "workspace_not_found"


def test_cursor_explicit_workspace_max_does_not_widen_existing_consumers():
    from devflow.pagination import CursorCodec

    codec = CursorCodec(b"k" * 32)
    actor = uuid4()
    token = codec.encode_cursor(
        actor, "workspace-tree:v1:scope", {"ordinal": 200}, 200, max_limit=200
    )
    assert codec.decode_cursor(
        token, actor, "workspace-tree:v1:scope", 200, max_limit=200
    ) == {"ordinal": 200}
    with pytest.raises(AppError):
        codec.decode_cursor(token, actor, "workspace-tree:v1:scope", 200)
    with pytest.raises(AppError):
        codec.encode_cursor(actor, "users", {}, 101)
