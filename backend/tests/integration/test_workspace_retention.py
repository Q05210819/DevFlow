"""Retention fences use real PostgreSQL and the native owned Git supervisor."""

import asyncio
import importlib
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from test_git_objects import objects as objects  # noqa: PLC0414
from test_workspace_prepare import prepare_env as prepare_env  # noqa: PLC0414
from test_workspace_prepare import request
from test_workspace_prepare import workspace_env as workspace_env  # noqa: PLC0414
from test_workspace_process import process_setup as process_setup  # noqa: PLC0414

from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.repositories.models import Repository
from devflow.workspace.guards import reserve_guard, revoke_reservation
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion


@pytest.fixture
def tmp_path(workspace_tmp_path):
    return workspace_tmp_path


async def claim(env):
    async with env.factory() as db:
        before = list(
            (
                await db.execute(
                    select(Job.status, Job.available_at, func.clock_timestamp())
                )
            ).all()
        )
    lease = await env.queue.claim(uuid4())
    if lease is None:
        async with env.factory() as db:
            after = list(
                (
                    await db.execute(
                        select(Job.status, Job.available_at, func.clock_timestamp())
                    )
                ).all()
            )
            guards = list(
                (
                    await db.execute(
                        select(WorkspaceProcessGuard.kind, WorkspaceProcessGuard.state)
                    )
                ).all()
            )
        pytest.fail(
            f"claim returned None; before={before}; after={after}; guards={guards}"
        )
    return lease


def assert_native_identity(identity, operation):
    assert identity["pid"] == operation.process.pid
    if os.name == "nt":
        assert identity["platform"] == "windows"
        assert identity["job"] == "Local\\DevFlow-" + operation.guard.operation_id
        assert identity["creation"] > 0
    else:
        assert identity["platform"] == "linux"
        assert int(identity["creation"]) > 0
        assert identity["session"] == identity["pid"]
        assert identity["boot"]
        assert identity["pid_namespace"] > 0


@pytest.mark.asyncio
async def test_native_bootstrap_budget_frame_preserves_exact_protocol(
    workspace_env, monkeypatch
):
    from devflow.workspace import process
    from devflow.workspace.git import GitCache
    from devflow.workspace.process import GitRunner

    env = workspace_env
    monkeypatch.setattr(process, "_WRITERS", threading.BoundedSemaphore(1))
    cache = GitCache(env.root / "cache", GitRunner(env.settings))
    view = await request(env)
    lease = await claim(env)
    deadline = time.monotonic() + 3
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="write",
        lease=lease,
        deadline=deadline,
        lock_path=cache.prepare_lock(101),
    )
    captured = []

    def run():
        with cache.operation(
            guard, deadline=deadline, cancel=threading.Event()
        ) as operation:
            captured.append(operation)
            assert cache.runner.bootstrap().status == "ok"

    await asyncio.to_thread(run)
    assert (cache.root / "101.git" / "config").is_file()
    assert captured[0].drained and captured[0].process.poll() is not None
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.nonce == guard.nonce and row.proof == "physical"
        assert_native_identity(row.identity, captured[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledge", [True, False])
async def test_native_closed_frame_preserves_proof_and_unknown_slot(
    workspace_env, monkeypatch, acknowledge
):
    import sys

    from devflow.workspace import process
    from devflow.workspace.git import GitCache
    from devflow.workspace.process import GitRunner, WorkspaceProcessError

    env = workspace_env
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(process, "_WRITERS", slot)
    cache = GitCache(env.root / "cache", GitRunner(env.settings))
    entry, trace = env.root / "protocol.py", env.root / "protocol.trace"
    entry.write_text(
        "from pathlib import Path\nimport devflow.workspace.process as p\n"
        f"trace=Path({str(trace)!r})\noriginal=p._write_frame\n"
        "def frame(pipe, value):"
        "\n    original(pipe,value)\n    with trace.open('a') as out: out.write(str({k:value.get(k) for k in ('op','status','active','drained')})+'\\n')\n"
        "p._write_frame=frame\n"
        + (
            "p._supervisor_main()\n"
            if acknowledge
            else "p._supervisor_main(drain=lambda guard: False)\n"
        )
        + "trace.write_text(trace.read_text()+';returned')\n"
    )
    monkeypatch.setattr(
        cache.runner, "_supervisor_command", lambda: [sys.executable, str(entry)]
    )
    view = await request(env)
    lease = await claim(env)
    deadline = time.monotonic() + 10
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="write",
        lease=lease,
        deadline=deadline,
        lock_path=cache.prepare_lock(101),
    )
    captured = []

    def run():
        try:
            with cache.operation(
                guard, deadline=deadline, cancel=threading.Event()
            ) as operation:
                captured.append(operation)
                cache.runner.run(
                    ["not-allowlisted"],
                    cwd=cache.root / "101.git",
                    env={},
                    timeout=1,
                    max_output=1024,
                    cancel=threading.Event(),
                )
        except WorkspaceProcessError:
            if not captured:
                raise
        else:
            pytest.fail("invalid command was accepted")

    await asyncio.to_thread(run)
    import subprocess

    try:
        await asyncio.to_thread(captured[0].process.wait, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"terminal trace={trace.read_text() if trace.exists() else 'missing'}; drained={captured[0].drained}; input_closed={captured[0].closed_input}"
        )
    assert captured[0].process.poll() is not None
    assert captured[0].drained is acknowledge
    acquired = slot.acquire(blocking=False)
    assert acquired is acknowledge
    if acquired:
        slot.release()
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.nonce == guard.nonce
        assert_native_identity(row.identity, captured[0])
        assert (row.state, row.proof) == (
            ("drained", "physical") if acknowledge else ("active", None)
        )


def retention(env):
    try:
        module = importlib.import_module("devflow.workspace.retention")
    except ModuleNotFoundError:
        pytest.fail("Workspace retention is not implemented")
    return module.WorkspaceRetention(env.factory, env.workspace.cache, env.settings)


async def prepared(env, key="first"):
    view = await request(env, key)
    await env.workspace.prepare(await claim(env))
    return view


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "write"])
@pytest.mark.parametrize("command", ["prune", "repack", "delete_pin"])
async def test_non_gc_supervisor_rejects_maintenance_mutations(
    prepare_env, kind, command
):
    from devflow.workspace.process import WorkspaceProcessError

    env = prepare_env
    view = await prepared(env)
    async with env.factory() as db:
        pin = (await db.get(WorkspaceVersion, view.id)).pin_ref
    authority = {"actor": env.actor}
    if kind == "write":
        view = await request(env, "second")
        authority = {"lease": await claim(env)}
    deadline = time.monotonic() + 5
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind=kind,
        deadline=deadline,
        lock_path=env.workspace.cache.lock_path(101),
        **authority,
    )
    args = {
        "prune": ["prune", "--expire=now"],
        "repack": ["repack", "-Ad"],
        "delete_pin": ["update-ref", "--no-deref", "-d", pin, env.commit],
    }[command]

    def run():
        cancel = threading.Event()
        # Enter the operation first: startup failure is not command rejection.
        with (
            env.workspace.cache.operation(guard, deadline=deadline, cancel=cancel),
            pytest.raises(WorkspaceProcessError, match="cleanup_required"),
        ):
            env.workspace.cache.runner.run(
                args,
                cwd=env.workspace.cache.root / "101.git",
                env={},
                timeout=4,
                max_output=1024,
                cancel=cancel,
            )

    await asyncio.to_thread(run)
    assert (
        env.workspace.cache.root / "101.git" / pin
    ).read_text().strip() == env.commit
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.state == "drained" and row.proof == "physical"


@pytest.mark.asyncio
async def test_write_supervisor_binds_pin_mutation_to_numeric_cache(prepare_env):
    from devflow.workspace.process import WorkspaceProcessError

    env = prepare_env
    await prepared(env)
    view = await request(env, "second")
    lease = await claim(env)
    outside = env.root / "other-owned-fixture"
    outside.mkdir()
    deadline = time.monotonic() + 5
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="write",
        lease=lease,
        deadline=deadline,
        lock_path=env.workspace.cache.lock_path(101),
    )

    def run():
        cancel = threading.Event()
        with (
            env.workspace.cache.operation(guard, deadline=deadline, cancel=cancel),
            pytest.raises(WorkspaceProcessError, match="cleanup_required"),
        ):
            env.workspace.cache.runner.run(
                [
                    "update-ref",
                    "--no-deref",
                    f"refs/devflow/pins/{guard.operation_id}",
                    env.commit,
                    "0" * 40,
                ],
                cwd=outside,
                env={},
                timeout=4,
                max_output=1024,
                cancel=cancel,
            )

    await asyncio.to_thread(run)
    assert list(outside.iterdir()) == []
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.state == "drained" and row.proof == "physical"


async def old(env, identity, *, current=False):
    async with env.factory.begin() as db:
        row = await db.get(WorkspaceVersion, identity)
        row.last_used_at = datetime.now(UTC) - timedelta(days=8)
        if not current:
            repo = await db.get(Repository, row.repository_id)
            repo.default_branch = "other"


@pytest.mark.asyncio
async def test_read_admission_refreshes_recency_before_physical_drain(prepare_env):
    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    async with env.factory() as db:
        before = await db.scalar(select(func.clock_timestamp()))
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="read",
        actor=env.actor,
        deadline=time.monotonic() + 5,
        lock_path=env.workspace.cache.lock_path(101),
    )
    try:
        async with env.factory() as db:
            row = await db.get(WorkspaceVersion, view.id)
            assert row.last_used_at >= before
    finally:
        await revoke_reservation(env.factory, guard)


@pytest.mark.asyncio
async def test_revoked_scope_still_allows_owned_gc_but_not_read(prepare_env):
    env = prepare_env
    view = await prepared(env)
    async with env.factory.begin() as db:
        row = await db.get(Repository, view.repository_id)
        row.available = False
        row.auth_version += 1
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="gc",
        deadline=time.monotonic() + 5,
        lock_path=env.workspace.cache.lock_path(101),
    )
    # The operation deadline must equal the reservation's absolute deadline.
    async with env.factory() as db:
        deadline = (await db.get(WorkspaceProcessGuard, guard.operation_id)).deadline

    def exact():
        with env.workspace.cache.operation(
            guard, deadline=deadline, cancel=threading.Event()
        ):
            pass

    await asyncio.to_thread(exact)
    async with env.factory() as db:
        assert (
            await db.get(WorkspaceProcessGuard, guard.operation_id)
        ).proof == "physical"
    with pytest.raises(AppError):
        await env.workspace.get(env.actor, view.repository_id, view.id)


@pytest.mark.asyncio
async def test_old_version_unavailable_before_removing_ref_and_identity_survives(
    prepare_env,
):
    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    result = await retention(env).sweep()
    assert result.evicted == 1
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        assert row.status == "unavailable" and row.pin_ref is None
        assert row.commit_sha == env.commit


@pytest.mark.asyncio
async def test_default_branch_and_reserved_reader_are_not_evicted(prepare_env):
    env = prepare_env
    view = await prepared(env)
    await old(env, view.id, current=True)
    assert (await retention(env).sweep()).evicted == 0
    await old(env, view.id)
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="read",
        actor=env.actor,
        deadline=time.monotonic() + 5,
        lock_path=env.workspace.cache.lock_path(101),
    )
    try:
        assert (await retention(env).sweep()).evicted == 0
    finally:
        await revoke_reservation(env.factory, guard)


@pytest.mark.asyncio
async def test_drain_to_publish_gap_keeps_exact_attempt_pin(prepare_env, monkeypatch):
    env = prepare_env
    env.settings.workspace_orphan_grace_seconds = 0
    view = await request(env)
    lease = await claim(env)
    arrived, release = asyncio.Event(), asyncio.Event()
    publish = env.workspace.publish

    async def paused(*args, **kwargs):
        arrived.set()
        await release.wait()
        return await publish(*args, **kwargs)

    monkeypatch.setattr(env.workspace, "publish", paused)
    work = asyncio.create_task(env.workspace.prepare(lease))
    await asyncio.wait_for(arrived.wait(), 10)
    try:
        result = await retention(env).sweep()
        assert (result.evicted, result.orphans, result.deferred) == (0, 0, 0)
        async with env.factory() as db:
            guard = await db.scalar(
                select(WorkspaceProcessGuard).where(
                    WorkspaceProcessGuard.workspace_id == view.id,
                    WorkspaceProcessGuard.kind == "write",
                )
            )
        assert guard.state == "drained" and guard.proof == "physical"
        assert (
            env.workspace.cache.root
            / "101.git"
            / f"refs/devflow/pins/{guard.operation_id}"
        ).exists()
    finally:
        release.set()
        await work
    assert (
        await env.workspace.get(env.actor, view.repository_id, view.id)
    ).status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("global_limit", [False, True])
async def test_residual_bytes_block_fetch_admission(prepare_env, global_limit):
    env = prepare_env
    residual = env.workspace.cache.root / ("999.git" if global_limit else "101.git")
    residual.mkdir(exist_ok=True)
    (residual / "residual.pack").write_bytes(b"x" * 2048)
    if global_limit:
        env.settings.workspace_total_bytes = 1024
    else:
        env.settings.workspace_repository_bytes = 1024
    view = await request(env)
    with pytest.raises(AppError) as caught:
        await env.workspace.prepare(await claim(env))
    assert caught.value.code == "storage_exhausted"
    assert (residual / "residual.pack").stat().st_size == 2048
    async with env.factory() as db:
        assert (await db.get(WorkspaceVersion, view.id)).status != "ready"


@pytest.mark.asyncio
async def test_health_positive_missing_pin_is_persistently_unavailable(prepare_env):
    from devflow.health.service import HealthService

    env = prepare_env
    view = await prepared(env)
    async with env.factory() as db:
        version = await db.get(WorkspaceVersion, view.id)
    (env.workspace.cache.root / "101.git" / version.pin_ref).unlink()
    health = HealthService(
        env.factory, env.policy, env.settings, cache=env.workspace.cache
    )
    await health.request(env.actor, view.repository_id, "health")
    frozen = await health.freeze(await claim(env))
    assert frozen.snapshot.workspace == "unavailable"
    assert frozen.snapshot.manifest.workspace_versions[0].availability == "unavailable"
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        assert row.status == "unavailable" and row.pin_ref == version.pin_ref


@pytest.mark.asyncio
async def test_health_available_identity_no_provider_calls_and_legacy_defaults(
    prepare_env,
    monkeypatch,
):
    from contextlib import contextmanager

    from devflow.health.schemas import HealthSnapshotView
    from devflow.health.service import HealthService
    from devflow.workspace.process import WorkspaceProcessError

    env = prepare_env
    view = await prepared(env)
    operation = env.workspace.cache.operation
    stages = []

    @contextmanager
    def observed(guard, **options):
        stages.append(("start", time.monotonic() - started))
        try:
            with operation(guard, **options) as native:
                stages.append(("ready", time.monotonic() - started))
                yield native
        except WorkspaceProcessError as error:
            stages.append((str(error), time.monotonic() - started))
            raise
        finally:
            stages.append(("closed", time.monotonic() - started))

    monkeypatch.setattr(env.workspace.cache, "operation", observed)
    health = HealthService(
        env.factory, env.policy, env.settings, cache=env.workspace.cache
    )
    await health.request(env.actor, view.repository_id, "health")
    before = len(env.data_calls)
    lease = await claim(env)
    started = time.monotonic()
    frozen = await health.freeze(lease)
    diagnostic = (stages, frozen.snapshot.manifest.workspace_selection)
    print(f"Health native stages/counts: {diagnostic}")
    assert len(env.data_calls) == before
    assert frozen.snapshot.workspace == "available", diagnostic
    assert frozen.snapshot.manifest.workspace_selection.checked_pins == 1
    assert frozen.snapshot.manifest.workspace_versions[0].commit_sha == env.commit
    old = frozen.snapshot.model_dump(mode="json")
    old["workspace"] = "unavailable"
    old["manifest"].pop("workspace_versions")
    old["manifest"].pop("workspace_selection")
    restored = HealthSnapshotView.model_validate(old)
    assert restored.manifest.workspace_versions == []
    assert restored.manifest.workspace_selection is None


@pytest.mark.asyncio
async def test_open_pr_protection_includes_evidence_beyond_health_cap(prepare_env):
    from devflow.facts.models import Fact

    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    async with env.factory.begin() as db:
        for number in range(1, 106):
            db.add(
                Fact(
                    repository_id=view.repository_id,
                    kind="pull",
                    source_id=number,
                    number=number,
                    state="open",
                    source_url=f"https://github.com/team/private/pull/{number}",
                    payload={
                        "title": "P",
                        "state": "open",
                        "issue_id": number,
                        "base_sha": "a" * 40,
                        "head_sha": env.commit if number == 105 else "b" * 40,
                        "draft": False,
                        "merged": False,
                    },
                )
            )
    assert (await retention(env).sweep()).evicted == 0
    async with env.factory() as db:
        assert (await db.get(WorkspaceVersion, view.id)).status == "ready"


@pytest.mark.asyncio
async def test_expired_unknown_reader_still_blocks_gc(prepare_env):
    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="read",
        actor=env.actor,
        deadline=time.monotonic() + 5,
        lock_path=env.workspace.cache.lock_path(101),
    )
    async with env.factory.begin() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        # Deliberately uncertain persisted state; no claim of a live fixture process.
        row.state, row.expires_at = "active", datetime(2000, 1, 1, tzinfo=UTC)
    result = await retention(env).sweep()
    assert result.evicted == 0 and result.deferred == 1
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.state == "active" and row.proof is None


@pytest.mark.asyncio
async def test_failed_orphan_requires_revocation_grace_and_physical_proof(
    prepare_env, monkeypatch
):
    env = prepare_env
    view = await request(env)
    lease = await claim(env)

    async def reject(*args, **kwargs):
        return False

    monkeypatch.setattr(env.workspace, "publish", reject)
    with pytest.raises(AppError):
        await env.workspace.prepare(lease)
    assert await env.queue.fail(lease, "permission_revoked", False)
    async with env.factory.begin() as db:
        guard = await db.scalar(
            select(WorkspaceProcessGuard).where(
                WorkspaceProcessGuard.workspace_id == view.id,
                WorkspaceProcessGuard.kind == "write",
            )
        )
        guard.drained_at = await db.scalar(select(func.clock_timestamp())) - timedelta(
            days=1
        )
    assert (await retention(env).sweep()).orphans == 0
    env.settings.workspace_orphan_grace_seconds = 0
    result = await retention(env).sweep()
    assert result.orphans == 1
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        assert row is not None and row.pin_ref is None


@pytest.mark.asyncio
async def test_interrupted_collection_keeps_unavailable_pin_for_resume(
    prepare_env, monkeypatch
):
    from devflow.workspace.process import WorkspaceProcessError

    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    original = env.workspace.cache.collect

    def fail(_repo):
        raise WorkspaceProcessError("git_failed")

    monkeypatch.setattr(env.workspace.cache, "collect", fail)
    assert (await retention(env).sweep()).deferred == 1
    async with env.factory() as db:
        row = await db.get(WorkspaceVersion, view.id)
        assert row.status == "unavailable" and row.pin_ref is not None
    with pytest.raises(AppError) as caught:
        await reserve_guard(
            env.factory,
            env.settings,
            view.id,
            kind="read",
            actor=env.actor,
            deadline=time.monotonic() + 5,
            lock_path=env.workspace.cache.lock_path(101),
        )
    assert caught.value.code == "version_unavailable"
    monkeypatch.setattr(env.workspace.cache, "collect", original)
    assert (await retention(env).sweep()).evicted == 1


@pytest.mark.asyncio
async def test_health_busy_is_unknown_without_invalidating_ready(prepare_env):
    from devflow.health.service import HealthService

    env = prepare_env
    view = await prepared(env)
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="read",
        actor=env.actor,
        deadline=time.monotonic() + 5,
        lock_path=env.workspace.cache.lock_path(101),
    )
    try:
        health = HealthService(
            env.factory, env.policy, env.settings, cache=env.workspace.cache
        )
        await health.request(env.actor, view.repository_id, "health")
        frozen = await health.freeze(await claim(env))
        assert frozen.snapshot.workspace == "unknown"
        assert (
            frozen.snapshot.manifest.workspace_versions[0].reason
            == "verification_unavailable"
        )
        summary = frozen.snapshot.manifest.workspace_selection
        assert (
            summary.checked_pins,
            summary.checked_count,
            summary.unverified_count,
        ) == (0, 0, 1)
        async with env.factory() as db:
            assert (await db.get(WorkspaceVersion, view.id)).status == "ready"
    finally:
        await revoke_reservation(env.factory, guard)


def test_growing_temporary_pack_is_stopped_by_supervisor_budget(
    process_setup, workspace_tmp_path
):
    import json

    runner, guard = process_setup
    runner.repository_bytes = 1024
    repo = workspace_tmp_path / "1.git"
    repo.mkdir(exist_ok=True)
    script = workspace_tmp_path / "grow.py"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        "Path('temporary.pack').write_bytes(b'x'*4096)\ntime.sleep(30)\n"
    )
    cancel = threading.Event()
    with runner.operation(
        guard, deadline=time.monotonic() + 5, cancel=cancel
    ) as operation:
        result = runner.run(
            [str(script)],
            cwd=repo,
            env={},
            timeout=4,
            max_output=1024,
            cancel=cancel,
        )
    assert result.status == "storage_exhausted"
    assert operation.drained
    # This fixture records the native lifecycle; it is not PostgreSQL proof.
    assert (
        json.loads((workspace_tmp_path / "guard.json").read_text())["state"]
        == "drained"
    )


@pytest.mark.asyncio
async def test_gc_rejects_command_outside_owned_numeric_cache(prepare_env):
    from devflow.workspace.process import WorkspaceProcessError

    env = prepare_env
    view = await prepared(env)
    outside = env.root / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("unchanged")
    deadline = time.monotonic() + 5
    guard = await reserve_guard(
        env.factory,
        env.settings,
        view.id,
        kind="gc",
        deadline=deadline,
        lock_path=env.workspace.cache.lock_path(101),
    )

    def run():
        cancel = threading.Event()
        with (
            env.workspace.cache.operation(guard, deadline=deadline, cancel=cancel),
            pytest.raises(WorkspaceProcessError, match="cleanup_required"),
        ):
            env.workspace.cache.runner.run(
                ["prune", "--expire=now"],
                cwd=outside,
                env={},
                timeout=4,
                max_output=1024,
                cancel=cancel,
            )

    await asyncio.to_thread(run)
    assert sentinel.read_text() == "unchanged"
    async with env.factory() as db:
        row = await db.get(WorkspaceProcessGuard, guard.operation_id)
        assert row.state == "drained" and row.proof == "physical"


@pytest.mark.asyncio
async def test_health_positive_missing_commit_is_unavailable(prepare_env):
    from devflow.health.service import HealthService

    env = prepare_env
    view = await prepared(env)
    obj = (
        env.workspace.cache.root
        / "101.git"
        / "objects"
        / env.commit[:2]
        / env.commit[2:]
    )
    assert obj.is_file()
    obj.unlink()
    health = HealthService(
        env.factory, env.policy, env.settings, cache=env.workspace.cache
    )
    await health.request(env.actor, view.repository_id, "health")
    frozen = await health.freeze(await claim(env))
    assert frozen.snapshot.workspace == "unavailable"
    summary = frozen.snapshot.manifest.workspace_selection
    assert (summary.checked_pins, summary.checked_count, summary.unverified_count) == (
        1,
        1,
        0,
    )


@pytest.mark.asyncio
async def test_health_logical_counts_deduplicate_pins_and_bound_checks(
    prepare_env, monkeypatch
):
    """Real DB selection/counting; inspection stub isolates the deterministic cap."""
    from contextlib import nullcontext

    from devflow.health.schemas import PRVersion
    from devflow.health.service import HealthService

    env = prepare_env
    view = await prepared(env)
    health = HealthService(
        env.factory, env.policy, env.settings, cache=env.workspace.cache
    )
    await health.request(env.actor, view.repository_id, "health")
    frozen = await health.freeze(await claim(env))
    async with env.factory() as db:
        original = await db.get(WorkspaceVersion, view.id)
    for index in range(18):
        extra = await request(env, f"candidate-{index}")
        async with env.factory.begin() as db:
            row = await db.get(WorkspaceVersion, extra.id)
            row.selection = {
                "kind": "pull",
                "number": index // 2 + 1,
                "side": "base" if index % 2 == 0 else "head",
            }
            row.status = "ready"
            row.pin_ref = (
                original.pin_ref if index == 0 else f"refs/devflow/pins/{uuid4().hex}"
            )
    pulls = [
        PRVersion(
            source_id=n,
            number=n,
            base_sha=env.commit,
            head_sha=env.commit,
            source_url=None,
            observed_at=frozen.snapshot.generated_at,
        )
        for n in range(10, 0, -1)
    ]
    snapshot = frozen.snapshot.model_copy(
        update={
            "manifest": frozen.snapshot.manifest.model_copy(update={"pulls": pulls})
        }
    )
    inspected = []
    monkeypatch.setattr(
        env.workspace.cache, "operation", lambda *a, **kw: nullcontext()
    )

    def inspect(*args):
        inspected.append(args[1])
        return "available"

    monkeypatch.setattr(env.workspace.cache, "inspect", inspect)
    maintenance = retention(env)
    observations = await maintenance.health_preflight(
        snapshot, view.repository_id, "main"
    )
    async with env.factory() as db:
        result = await maintenance.health_evidence(db, snapshot, "main", observations)
    summary = result.manifest.workspace_selection
    assert result.workspace == "partial"
    assert summary.total_candidates == summary.returned_count == 21
    assert (summary.checked_pins, summary.checked_count, summary.unverified_count) == (
        16,
        17,
        2,
    )
    assert len(inspected) == len(set(inspected)) == 16
    entries = result.manifest.workspace_versions
    assert all(
        entry.reason == "verification_limit"
        for entry in entries
        if entry.availability == "unknown"
    )
    assert entries[1].selection.number == 1 and entries[1].selection.side == "base"
    assert [entry.availability for entry in entries[-2:]] == ["unprepared"] * 2


@pytest.mark.asyncio
async def test_gc_rejects_nested_reparse_before_any_ref_removal(prepare_env):
    import os

    env = prepare_env
    view = await prepared(env)
    await old(env, view.id)
    outside = env.root / "foreign-objects"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("unchanged")
    repo = env.workspace.cache.root / "101.git"
    junction = repo / "objects" / "ff"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(junction))
    else:
        junction.symlink_to(outside, target_is_directory=True)
    async with env.factory() as db:
        pin = (await db.get(WorkspaceVersion, view.id)).pin_ref
    result = await retention(env).sweep()
    assert result.evicted == 0 and result.deferred == 1
    assert sentinel.read_text() == "unchanged"
    assert (repo / pin).is_file()
