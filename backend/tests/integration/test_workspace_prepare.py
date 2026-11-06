"""Real PostgreSQL authorization/fencing with synthetic GitHub transport."""

import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from support.repository_env import clear_initial_sync, repository
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from test_git_objects import objects as objects  # noqa: PLC0414

from devflow.errors import AppError
from devflow.github.client import GitHubClient
from devflow.github.data import GitHubDataClient
from devflow.jobs.models import Job, RunEvent
from devflow.jobs.queue import Queue

SHA = "a" * 40
OTHER = "b" * 40
TREE = "c" * 40


@pytest.fixture
def tmp_path(workspace_tmp_path):
    # Only this module opts its synthetic credentials into the owned fixture.
    return workspace_tmp_path


@pytest_asyncio.fixture
async def workspace_env(repo_env, workspace_tmp_path):
    from devflow.workspace.service import WorkspaceService

    env = repo_env
    env.repository = await env.service.register(env.actor, 11, 101)
    await clear_initial_sync(env)
    env.metadata = {
        "/repos/team/private/git/ref/heads/main": {
            "ref": "refs/heads/main",
            "object": {"type": "commit", "sha": SHA},
        },
        f"/repos/team/private/git/commits/{SHA}": {"sha": SHA, "tree": {"sha": TREE}},
        f"/repos/team/private/git/commits/{OTHER}": {
            "sha": OTHER,
            "tree": {"sha": TREE},
        },
    }
    env.data_calls = []

    async def remote(request):
        env.data_calls.append(request)
        value = env.metadata.get(request.url.path)
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(404 if value is None else 200, json=value or {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        env.workspace = WorkspaceService(
            env.factory,
            env.policy,
            env.settings,
            data=GitHubDataClient(GitHubClient(http, env.settings)),
        )
        env.queue = Queue(env.factory, settings=env.settings)
        env.root = workspace_tmp_path
        yield env


async def request(env, key="first", selection=None):
    return await env.workspace.request(
        env.actor,
        env.repository.id,
        selection or {"kind": "branch", "name": "main"},
        key,
    )


async def claim(env):
    from devflow.workspace.models import WorkspaceProcessGuard

    async def snapshot():
        async with env.factory() as db:
            jobs = list(
                (
                    await db.execute(
                        select(
                            Job.id,
                            Job.kind,
                            Job.status,
                            Job.available_at,
                            Job.attempt,
                            Job.max_attempts,
                            Job.lease_owner,
                            Job.lease_generation,
                            Job.lease_expires_at,
                            Job.error_code,
                            func.clock_timestamp(),
                        )
                    )
                ).all()
            )
            eligible = list(
                await db.scalars(
                    select(Job.id).where(
                        Job.status.in_(("queued", "retry_wait")),
                        Job.available_at <= func.clock_timestamp(),
                        Job.attempt < Job.max_attempts,
                    )
                )
            )
            guards = list(
                (
                    await db.execute(
                        select(
                            WorkspaceProcessGuard.operation_id,
                            WorkspaceProcessGuard.kind,
                            WorkspaceProcessGuard.state,
                            WorkspaceProcessGuard.expires_at,
                        )
                    )
                ).all()
            )
        return {"jobs": jobs, "eligible": eligible, "guards": guards}

    before = await snapshot()
    lease = await env.queue.claim(uuid4())
    if lease is None or os.environ.get("DEVFLOW_TEST_CLAIM_AUDIT"):
        after = await snapshot()
        diagnostic = f"claim before={before}; after={after}; lease={lease}"
        if lease is None:
            pytest.fail(diagnostic)
        print(diagnostic)
    return lease


@pytest.mark.asyncio
async def test_branch_replay_freezes_sha_and_changed_selection_conflicts(workspace_env):
    env = workspace_env
    first = await request(env)
    env.metadata["/repos/team/private/git/ref/heads/main"]["object"]["sha"] = OTHER
    before = len(env.data_calls)
    replay = await request(env)
    assert replay.id == first.id and replay.commit_sha == SHA
    assert len(env.data_calls) == before
    later = await request(env, "later")
    assert later.id != first.id and later.commit_sha == OTHER
    with pytest.raises(AppError) as caught:
        await request(env, selection={"kind": "tag", "name": "main"})
    assert caught.value.code == "idempotency_conflict"
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [
        {"kind": "sha", "sha": SHA},
        {"kind": "branch", "name": "main~1"},
        {"kind": "branch", "name": "../main"},
        {"kind": "ci", "run_id": True, "attempt": 1},
    ],
)
async def test_invalid_selection_never_creates_job(workspace_env, selection):
    env = workspace_env
    with pytest.raises(AppError) as caught:
        await request(env, selection=selection)
    assert caught.value.code == "validation_error"
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.asyncio
async def test_concurrent_idempotent_request_and_global_workspace_claim(workspace_env):
    env = workspace_env
    first, replay = await asyncio.gather(request(env), request(env))
    assert first.id == replay.id
    await request(env, "second")
    leases = await asyncio.gather(env.queue.claim(uuid4()), env.queue.claim(uuid4()))
    assert sum(value is not None for value in leases) == 1


async def fork(env):
    env.repositories[(11, 1)][0].append(repository(102, "team/fork"))
    foreign = await env.service.register(env.actor, 11, 102)
    original = env.repository
    env.repository = foreign
    await clear_initial_sync(env)
    env.repository = original
    env.metadata["/repos/team/private/pulls/7"] = {
        "id": 70,
        "number": 7,
        "base": {"repo": {"id": 101}, "ref": "main", "sha": SHA},
        "head": {"repo": {"id": 102}, "ref": "topic", "sha": OTHER},
    }
    env.metadata[f"/repos/team/fork/git/commits/{OTHER}"] = {
        "sha": OTHER,
        "tree": {"sha": TREE},
    }
    return foreign


@pytest.mark.asyncio
async def test_fork_source_revocation_hides_head_but_preserves_base_and_list(
    workspace_env,
):
    from devflow.repositories.models import Repository

    env = workspace_env
    foreign = await fork(env)
    base = await request(env, "base", {"kind": "pull", "number": 7, "side": "base"})
    head = await request(env, "head", {"kind": "pull", "number": 7, "side": "head"})
    assert base.commit_sha == SHA and head.commit_sha == OTHER
    async with env.factory.begin() as db:
        row = await db.get(Repository, foreign.id)
        row.available = False
        row.auth_version += 1
    with pytest.raises(AppError) as caught:
        await env.workspace.get(env.actor, env.repository.id, head.id)
    assert caught.value.code == "fork_source_unavailable"
    assert (
        await env.workspace.get(env.actor, env.repository.id, base.id)
    ).id == base.id
    page = await env.workspace.list(env.actor, env.repository.id, limit=1)
    assert page.items == [] and page.unavailable_count == 1 and page.has_more
    next_page = await env.workspace.list(
        env.actor, env.repository.id, limit=1, cursor=page.next_cursor
    )
    assert [item.id for item in next_page.items] == [base.id]


@pytest.mark.asyncio
async def test_ci_exact_attempt_rejects_current_pr_head_substitution(workspace_env):
    env = workspace_env
    await fork(env)
    env.metadata["/repos/team/private/actions/runs/55/attempts/1"] = {
        "id": 55,
        "run_attempt": 1,
        "repository": {"id": 101},
        "head_repository": {"id": 102},
        "head_sha": SHA,
        "head_branch": "topic",
        "pull_requests": [{"number": 7}],
    }
    with pytest.raises(AppError) as caught:
        await request(env, selection={"kind": "ci", "run_id": 55, "attempt": 1})
    assert caught.value.code == "version_unavailable"
    env.metadata["/repos/team/private/actions/runs/55/attempts/1"]["head_sha"] = OTHER
    view = await request(env, selection={"kind": "ci", "run_id": 55, "attempt": 1})
    assert view.commit_sha == OTHER


@pytest.mark.asyncio
async def test_guard_nonce_expiry_and_physical_ack_are_independent_of_job_authority(
    workspace_env,
):
    from devflow.workspace.guards import activate_guard, drain_guard, reserve_guard
    from devflow.workspace.models import WorkspaceProcessGuard
    from devflow.workspace.process import _identity

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="write",
        deadline=time.monotonic() + 20,
        lease=lease,
        lock_path=env.root / ".locks" / "101.lock",
    )
    payload = asdict(guard)
    payload["lock_path"] = str(payload["lock_path"])
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        payload["deadline"] = row.deadline
    identity = _identity("Local\\DevFlow-" + guard.operation_id)
    assert await asyncio.to_thread(activate_guard, payload, identity)
    assert not await asyncio.to_thread(activate_guard, payload, identity)
    async with env.factory.begin() as db:
        await db.execute(text("UPDATE jobs SET lease_expires_at='2000-01-01 UTC'"))
        await db.execute(
            text("UPDATE workspace_process_guards SET expires_at='2000-01-01 UTC'")
        )
    with pytest.raises(AppError) as caught:
        await reserve_guard(
            env.factory,
            env.settings,
            view.id,
            kind="read",
            deadline=time.monotonic() + 2,
            actor=env.actor,
            lock_path=guard.lock_path,
        )
    assert caught.value.code in {"workspace_busy", "version_unavailable"}
    payload["cleanup_deadline"] = time.monotonic() + 1.5
    assert not await asyncio.to_thread(drain_guard, {**payload, "nonce": uuid4().hex})
    assert await asyncio.to_thread(drain_guard, payload)
    async with env.factory() as db:
        assert (
            await db.get(WorkspaceProcessGuard, guard.operation_id)
        ).state == "drained"


@pytest.mark.asyncio
async def test_stale_lease_cannot_publish_ready_snapshot(workspace_env):
    from devflow.workspace.git import GitCommit

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)
    bad = replace(lease, generation=lease.generation + 1)
    assert not await env.workspace.publish(
        bad,
        GitCommit(SHA, TREE, "refs/devflow/pins/" + guard.operation_id),
        guard=guard,
    )
    current = await env.workspace.get(env.actor, env.repository.id, view.id)
    assert current.status != "ready"
    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.payload["status"].astext == "succeeded")
            )
            == 0
        )


async def reservation(env, view, lease, seconds=10):
    from devflow.workspace.guards import reserve_guard
    from devflow.workspace.models import WorkspaceProcessGuard

    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="write",
        deadline=time.monotonic() + seconds,
        lease=lease,
        lock_path=env.root / ".locks" / "101.lock",
    )
    payload = asdict(guard)
    payload["lock_path"] = str(guard.lock_path)
    async with env.factory() as db:
        payload["deadline"] = (
            await db.get(WorkspaceProcessGuard, guard.operation_id)
        ).deadline
    return guard, payload


async def physical_guard(env, view, lease):
    from devflow.workspace.guards import activate_guard, drain_guard
    from devflow.workspace.process import _identity

    guard, payload = await reservation(env, view, lease)
    identity = _identity("Local\\DevFlow-" + guard.operation_id)
    assert await asyncio.to_thread(activate_guard, payload, identity)
    payload["cleanup_deadline"] = time.monotonic() + 1.5
    assert await asyncio.to_thread(drain_guard, payload)
    return guard


@pytest.mark.asyncio
async def test_publication_transaction_failure_rolls_back_ready_job_and_event(
    workspace_env, monkeypatch
):
    from devflow.workspace.git import GitCommit
    from devflow.workspace.models import WorkspaceVersion

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)

    async def fail_transition(*args):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(Queue, "_transition", fail_transition)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await env.workspace.publish(
            lease,
            GitCommit(SHA, TREE, "refs/devflow/pins/" + guard.operation_id),
            guard=guard,
        )
    async with env.factory() as db:
        version = await db.get(WorkspaceVersion, view.id)
        job = await db.get(Job, lease.job_id)
        assert version.status == "queued" and version.pin_ref is None
        assert job.status == "running" and job.result is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.payload["status"].astext == "succeeded")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_workspace_downgrade_preserves_immutable_provenance(
    workspace_env, migrated_database
):
    from alembic import command

    from devflow.workspace.models import WorkspaceVersion

    env = workspace_env
    view = await request(env)
    with pytest.raises(RuntimeError, match="Cannot discard workspace provenance"):
        command.downgrade(migrated_database[1], "0008_sync_repair")
    async with env.factory() as db:
        assert (await db.get(WorkspaceVersion, view.id)).commit_sha == SHA
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == "0009_workspace"
        )


@pytest.mark.asyncio
async def test_cancelled_owned_thread_waits_until_cleanup_finishes():
    import threading

    from devflow.workspace.handlers import owned_thread

    cancel, started, release, finished = (threading.Event() for _ in range(4))

    def operation():
        started.set()
        assert release.wait(3)
        finished.set()

    task = asyncio.create_task(owned_thread(operation, cancel))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert cancel.is_set() and not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_malformed_provider_object_is_unavailable_not_internal_error(
    workspace_env,
):
    env = workspace_env
    await fork(env)
    env.metadata["/repos/team/private/pulls/7"]["head"]["repo"] = "invalid"
    with pytest.raises(AppError) as caught:
        await request(env, "malformed", {"kind": "pull", "number": 7, "side": "head"})
    assert caught.value.code == "version_unavailable"


@pytest.mark.asyncio
async def test_activation_rechecks_lease_after_workspace_lock_wait(workspace_env):
    from devflow.workspace.guards import activate_guard
    from devflow.workspace.models import WorkspaceVersion
    from devflow.workspace.process import _identity

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard, payload = await reservation(env, view, lease)
    identity = _identity("Local\\DevFlow-" + guard.operation_id)
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '0.2 seconds'"
            )
        )
    async with env.factory.begin() as blocker:
        await blocker.get(WorkspaceVersion, view.id, with_for_update=True)
        pending = asyncio.create_task(
            asyncio.to_thread(activate_guard, payload, identity)
        )
        await blocker.execute(text("SELECT pg_sleep(0.35)"))
    assert not await pending


@pytest.mark.asyncio
async def test_publication_rechecks_lease_after_guard_lock_wait(workspace_env):
    from devflow.workspace.git import GitCommit
    from devflow.workspace.models import WorkspaceProcessGuard

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '0.2 seconds'"
            )
        )
    async with env.factory.begin() as blocker:
        await blocker.get(
            WorkspaceProcessGuard, guard.operation_id, with_for_update=True
        )
        pending = asyncio.create_task(
            env.workspace.publish(
                lease,
                GitCommit(SHA, TREE, "refs/devflow/pins/" + guard.operation_id),
                guard=guard,
            )
        )
        await blocker.execute(text("SELECT pg_sleep(0.35)"))
    assert not await pending
    assert (
        await env.workspace.get(env.actor, env.repository.id, view.id)
    ).status != "ready"


@pytest.mark.asyncio
async def test_publication_cannot_adopt_another_attempt_pin(workspace_env):
    from devflow.workspace.git import GitCommit

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)
    with pytest.raises(AppError):
        await env.workspace.publish(
            lease, GitCommit(SHA, TREE, "refs/devflow/pins/" + uuid4().hex), guard=guard
        )
    assert (
        await env.workspace.get(env.actor, env.repository.id, view.id)
    ).status != "ready"


@pytest.mark.asyncio
async def test_four_readers_are_global_and_expired_active_read_still_excludes_writer(
    workspace_env,
):
    from devflow.workspace.git import GitCommit
    from devflow.workspace.guards import activate_guard, reserve_guard
    from devflow.workspace.process import _identity

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    physical = await physical_guard(env, view, lease)
    assert await env.workspace.publish(
        lease,
        GitCommit(SHA, TREE, "refs/devflow/pins/" + physical.operation_id),
        guard=physical,
    )

    async def read():
        return await reserve_guard(
            env.factory,
            env.settings,
            view.id,
            kind="read",
            deadline=time.monotonic() + 5,
            actor=env.actor,
            lock_path=physical.lock_path,
        )

    results = await asyncio.gather(*(read() for _ in range(5)), return_exceptions=True)
    readers = [result for result in results if not isinstance(result, Exception)]
    assert len(readers) == 4
    assert [result.code for result in results if isinstance(result, AppError)] == [
        "workspace_busy"
    ]
    payload = asdict(readers[0])
    payload.update(lock_path=str(readers[0].lock_path))
    from devflow.workspace.models import WorkspaceProcessGuard

    async with env.factory() as db:
        payload["deadline"] = (
            await db.get(WorkspaceProcessGuard, readers[0].operation_id)
        ).deadline
    assert await asyncio.to_thread(
        activate_guard, payload, _identity("Local\\DevFlow-" + readers[0].operation_id)
    )
    async with env.factory.begin() as db:
        await db.execute(
            text(
                "UPDATE workspace_process_guards SET expires_at='2000-01-01 UTC' WHERE kind='read'"
            )
        )
    later = await request(env, "later")
    next_lease = await claim(env)
    with pytest.raises(AppError) as caught:
        await reserve_guard(
            env.factory,
            env.settings,
            later.id,
            kind="write",
            deadline=time.monotonic() + 5,
            lease=next_lease,
            lock_path=physical.lock_path,
        )
    assert caught.value.code == "workspace_busy"


@pytest.mark.asyncio
async def test_callback_lock_timeout_does_not_clear_reservation(workspace_env):
    from devflow.workspace.guards import activate_guard
    from devflow.workspace.models import WorkspaceProcessGuard
    from devflow.workspace.process import _identity

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard, payload = await reservation(env, view, lease, seconds=0.4)
    async with env.factory.begin() as db:
        await db.get(WorkspaceProcessGuard, guard.operation_id, with_for_update=True)
        started = time.monotonic()
        result = await asyncio.to_thread(
            activate_guard, payload, _identity("Local\\DevFlow-" + guard.operation_id)
        )
        assert not result and time.monotonic() - started < 1.5
    async with env.factory() as db:
        assert (
            await db.get(WorkspaceProcessGuard, guard.operation_id)
        ).state == "reserved"


@pytest.mark.asyncio
async def test_another_authorized_member_reads_ready_snapshot_after_creator_disabled(
    workspace_env,
):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.models import GitHubCredential
    from devflow.workspace.git import GitCommit

    env = workspace_env
    await fork(env)
    view = await request(env, "shared", {"kind": "pull", "number": 7, "side": "head"})
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)
    async with env.factory.begin() as db:
        creator = await db.get(User, env.actor.id)
        creator.enabled = False
        member = User(github_id=77, role="member")
        db.add(member)
        await db.flush()
        db.add(
            GitHubCredential(
                user_id=member.id,
                access_token_encrypted=env.cipher.encrypt(SecretStr("member-token")),
            )
        )
        actor = Actor(member.id, 77, "member", member.auth_version)
    assert await env.workspace.publish(
        lease,
        GitCommit(OTHER, TREE, "refs/devflow/pins/" + guard.operation_id),
        guard=guard,
    )
    shared = await env.workspace.get(actor, env.repository.id, view.id)
    assert shared.id == view.id and shared.status == "ready"
    assert [
        item.id for item in (await env.workspace.list(actor, env.repository.id)).items
    ] == [view.id]
    serialized = shared.model_dump_json()
    assert all(
        word not in serialized
        for word in ("pin_ref", "nonce", "database_url", "member-token")
    )
    env.policy._cache.clear()
    env.repositories[(11, 1)] = ([repository()], None)
    with pytest.raises(AppError) as caught:
        await env.workspace.get(actor, env.repository.id, view.id)
    assert caught.value.code == "fork_source_unavailable"


@pytest_asyncio.fixture
async def prepare_env(workspace_env, objects, monkeypatch):
    from devflow.workspace.process import GitRunner, ProcessResult

    env = workspace_env
    cache, _, sha, *_rest = objects
    runner = cache.runner
    # Keep synthetic transfer only; run the real production supervisor and DB
    # callbacks for bootstrap, Git object verification, immutable pin and drain.
    monkeypatch.setattr(
        runner, "_supervisor_command", GitRunner._supervisor_command.__get__(runner)
    )
    real_run = GitRunner.run.__get__(runner)
    source = objects[5]

    def transfer(args, **kwargs):
        if args[0] != "fetch":
            return real_run(args, **kwargs)
        destination = kwargs["cwd"]
        for original in (source / "objects").rglob("*"):
            target = destination / "objects" / original.relative_to(source / "objects")
            if original.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
        source_ref, target_ref = args[-1].removeprefix("+").split(":")
        target = destination / target_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / source_ref).read_bytes())
        return ProcessResult("ok", returncode=0)

    monkeypatch.setattr(runner, "run", transfer)
    env.workspace.cache = cache
    env.commit = sha
    env.tree = objects[-1]("cat-file", "commit", sha).split(b"\n", 1)[0][5:].decode()
    env.metadata["/repos/team/private/git/ref/heads/main"]["object"]["sha"] = sha
    env.metadata[f"/repos/team/private/git/commits/{sha}"] = {
        "sha": sha,
        "tree": {"sha": env.tree},
    }
    return env


@pytest.mark.asyncio
async def test_prepare_real_supervisor_db_guard_pin_and_atomic_job_publication(
    prepare_env,
):
    from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion

    env = prepare_env
    view = await request(env)
    lease = await claim(env)
    result = await env.workspace.prepare(lease)
    assert result.workspace_id == view.id and result.commit_sha == env.commit
    final = await env.workspace.get(env.actor, env.repository.id, view.id)
    assert final.status == "ready"
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        physical = await db.scalar(select(WorkspaceProcessGuard))
        job = await db.get(Job, lease.job_id)
        assert physical.state == "drained" and physical.proof == "physical"
        assert row.pin_ref == "refs/devflow/pins/" + physical.operation_id
        assert job.status == "succeeded" and job.result["workspace_id"] == str(view.id)
        assert [
            event.payload["status"]
            for event in await db.scalars(
                select(RunEvent)
                .where(RunEvent.job_id == job.id)
                .order_by(RunEvent.sequence)
            )
        ] == ["queued", "running", "succeeded"]
    assert (
        env.workspace.cache.root / "101.git" / row.pin_ref
    ).read_text().strip() == env.commit
    requests = [json.loads(call.content) for call in env.calls if call.method == "POST"]
    assert requests and all(
        item["repository_ids"] == [101] and item["permissions"] == {"contents": "read"}
        for item in requests
    )


@pytest.mark.asyncio
async def test_fetched_ref_race_cannot_publish_or_fallback(prepare_env, monkeypatch):
    from devflow.workspace.models import WorkspaceVersion

    env = prepare_env
    view = await request(env)
    lease = await claim(env)
    real_fetch = env.workspace.cache.fetch

    def changed(*args, **kwargs):
        result = real_fetch(*args, **kwargs)
        return replace(result, sha=OTHER)

    monkeypatch.setattr(env.workspace.cache, "fetch", changed)
    with pytest.raises(AppError) as caught:
        await env.workspace.prepare(lease)
    assert caught.value.code == "version_changed"
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        assert row.pin_ref is None and row.commit_sha == env.commit
        assert (await db.get(Job, lease.job_id)).status == "running"


@pytest.mark.asyncio
async def test_database_failure_after_pin_recovers_with_new_attempt(
    prepare_env, monkeypatch
):
    from devflow.workspace.models import WorkspaceProcessGuard

    env = prepare_env
    view = await request(env)
    lease = await claim(env)
    real_publish = env.workspace.publish

    async def database_failure(*args, **kwargs):
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(env.workspace, "publish", database_failure)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        await env.workspace.prepare(lease)
    await env.queue.fail(lease, "github_unavailable", True)
    async with env.factory.begin() as db:
        await db.execute(text("UPDATE jobs SET available_at='2000-01-01 UTC'"))
    retry = await claim(env)
    monkeypatch.setattr(env.workspace, "publish", real_publish)
    await env.workspace.prepare(retry)
    async with env.factory() as db:
        guards = list(await db.scalars(select(WorkspaceProcessGuard)))
        assert len(guards) == 2 and len({guard.operation_id for guard in guards}) == 2
        assert all(guard.state == "drained" for guard in guards)
    for guard in guards:
        assert (
            env.workspace.cache.root
            / "101.git"
            / "refs"
            / "devflow"
            / "pins"
            / guard.operation_id
        ).read_text().strip() == env.commit
    assert (
        await env.workspace.get(env.actor, env.repository.id, view.id)
    ).status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["deleted", "contents"])
async def test_foreign_source_gaps_have_fork_error_without_creating_workspace(
    workspace_env, missing
):
    env = workspace_env
    await fork(env)
    if missing == "deleted":
        env.metadata["/repos/team/private/pulls/7"]["head"]["repo"] = None
    else:
        env.metadata[f"/repos/team/fork/git/commits/{OTHER}"] = httpx.Response(
            403, json={}
        )
    with pytest.raises(AppError) as caught:
        await request(env, selection={"kind": "pull", "number": 7, "side": "head"})
    assert caught.value.code == "fork_source_unavailable"
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [0, 1, 8, 9])
async def test_tag_peeling_is_bounded_and_preserves_exact_selection(
    workspace_env, depth
):
    env = workspace_env
    first = f"{1:040x}" if depth else SHA
    env.metadata["/repos/team/private/git/ref/tags/v1"] = {
        "ref": "refs/tags/v1",
        "object": {"type": "tag" if depth else "commit", "sha": first},
    }
    for index in range(1, depth + 1):
        oid = f"{index:040x}"
        env.metadata[f"/repos/team/private/git/tags/{oid}"] = {
            "sha": oid,
            "object": {
                "type": "tag" if index < depth else "commit",
                "sha": f"{index + 1:040x}" if index < depth else SHA,
            },
        }
    if depth > 8:
        with pytest.raises(AppError) as caught:
            await request(env, selection={"kind": "tag", "name": "v1"})
        assert caught.value.code == "version_unavailable"
    else:
        view = await request(env, selection={"kind": "tag", "name": "v1"})
        assert view.commit_sha == SHA and view.source.ref == "refs/tags/v1"


@pytest.mark.asyncio
async def test_literal_ref_is_encoded_once_and_prefix_match_is_rejected(workspace_env):
    env = workspace_env
    name = "release/#%2F"
    path = "/repos/team/private/git/ref/heads/" + name
    env.metadata[path] = {
        "ref": "refs/heads/" + name,
        "object": {"type": "commit", "sha": SHA},
    }
    view = await request(env, selection={"kind": "branch", "name": name})
    assert view.source.ref == "refs/heads/" + name
    assert env.data_calls[0].url.raw_path.endswith(b"release/%23%252F")
    env.metadata[path]["ref"] += "suffix"
    with pytest.raises(AppError) as caught:
        await request(env, "other", {"kind": "branch", "name": name})
    assert caught.value.code == "version_unavailable"


@pytest.mark.asyncio
async def test_prepared_content_does_not_merge_distinct_source_provenance(
    workspace_env,
):
    env = workspace_env
    await fork(env)
    env.metadata["/repos/team/private/pulls/7"]["head"]["sha"] = SHA
    env.metadata[f"/repos/team/fork/git/commits/{SHA}"] = {
        "sha": SHA,
        "tree": {"sha": TREE},
    }
    branch = await request(env)
    head = await request(env, "pr", {"kind": "pull", "number": 7, "side": "head"})
    assert branch.commit_sha == head.commit_sha and branch.id != head.id
    assert branch.source_repository_id != head.source_repository_id


@pytest.mark.asyncio
async def test_installation_epoch_change_blocks_publication_after_physical_drain(
    workspace_env,
):
    from devflow.github.models import GitHubInstallation
    from devflow.workspace.git import GitCommit

    env = workspace_env
    view = await request(env)
    lease = await claim(env)
    guard = await physical_guard(env, view, lease)
    async with env.factory.begin() as db:
        installation = await db.scalar(
            select(GitHubInstallation).where(GitHubInstallation.installation_id == 11)
        )
        installation.auth_version += 1
    assert not await env.workspace.publish(
        lease,
        GitCommit(SHA, TREE, "refs/devflow/pins/" + guard.operation_id),
        guard=guard,
    )
    assert not await env.queue.complete(
        lease, SimpleNamespace(workspace_id=view.id, commit_sha=SHA)
    )


@pytest.mark.asyncio
async def test_list_cursor_scope_and_authentication_failure_do_not_leak(workspace_env):
    from devflow.auth.models import User

    env = workspace_env
    await request(env)
    await request(env, "next")
    first = await env.workspace.list(env.actor, env.repository.id, limit=1)
    with pytest.raises(AppError) as caught:
        await env.workspace.list(
            env.actor, env.repository.id, limit=2, cursor=first.next_cursor
        )
    assert caught.value.code == "invalid_cursor"
    async with env.factory.begin() as db:
        user = await db.get(User, env.actor.id)
        user.enabled = False
    with pytest.raises(AppError) as caught:
        await env.workspace.list(
            env.actor, env.repository.id, limit=1, cursor=first.next_cursor
        )
    assert caught.value.code == "unauthenticated"
