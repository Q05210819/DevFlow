"""Real PG retention selection; no native process or physical-GC proof claims."""

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from test_workspace_prepare import request
from test_workspace_prepare import workspace_env as workspace_env  # noqa: PLC0414

from devflow.facts.models import Fact
from devflow.health.schemas import HealthSnapshotView, PRVersion
from devflow.jobs.models import Job
from devflow.workspace.guards import reserve_guard, revoke_reservation
from devflow.workspace.models import WorkspaceProcessGuard, WorkspaceVersion
from devflow.workspace.retention import RetentionResult, WorkspaceRetention

OLD = datetime(2000, 1, 1, tzinfo=UTC)


async def seed(env, count):
    """Clone a real authorized request into explicitly old, synthetic DB evidence."""
    view = await request(env)
    async with env.factory.begin() as db:
        original = await db.get(WorkspaceVersion, view.id)
        values = {
            c.name: getattr(original, c.name)
            for c in WorkspaceVersion.__table__.columns
        }
        jobs, versions = [], []
        for index in range(count):
            job_id = uuid4()
            jobs.append(
                {
                    "id": job_id,
                    "kind": "repository.workspace",
                    "repository_id": view.repository_id,
                    "input": {},
                    "input_hash": "a" * 64,
                    "idempotency_key": f"history-{index}",
                    "status": "succeeded",
                    "updated_at": OLD,
                }
            )
            versions.append(
                {
                    **values,
                    "id": UUID(int=index + 1),
                    "job_id": job_id,
                    "idempotency_key": f"history-{index}",
                    "status": "unavailable",
                    "pin_ref": None,
                    "created_at": OLD,
                    "last_used_at": OLD,
                }
            )
        await db.execute(insert(Job), jobs)
        await db.execute(insert(WorkspaceVersion), versions)
    return view, versions


def guard(version, index, **changes):
    return dict(
        operation_id=f"{index:032x}",
        nonce=uuid4().hex,
        repository_id=101,
        workspace_id=version["id"],
        kind="write",
        state="drained",
        runtime_id=uuid4(),
        lock_path="synthetic-no-process",
        deadline=0,
        expires_at=OLD,
        scopes=version["scopes"],
        lease={
            "job_id": str(version["job_id"]),
            "owner": str(UUID(int=1)),
            "generation": 1,
            "attempt": 1,
        },
        proof="physical",
        drained_at=OLD,
        **changes,
    )


@pytest.mark.asyncio
async def test_sweep_progresses_past_permanent_history_and_historical_write_guards(
    workspace_env,
):
    env = workspace_env
    _view, versions = await seed(env, 1005)
    async with env.factory.begin() as db:
        await db.execute(
            insert(WorkspaceProcessGuard),
            [guard(v, i + 1) for i, v in enumerate(versions)],
        )
        # All historical guards remain referenced by permanent evidence, including
        # non-ready rows. They must neither stop pagination nor become orphans.
        for i, values in enumerate(versions):
            row = await db.get(WorkspaceVersion, values["id"])
            row.pin_ref = f"refs/devflow/pins/{i + 1:032x}"
            row.status = "failed"
        eligible = await db.get(WorkspaceVersion, versions[-1]["id"])
        eligible.status = "ready"
        eligible.selection = {"kind": "tag", "name": "old"}
    # The native boundary is replaced, not its admission/metadata transactions.
    # Recording removals proves scheduling only; these are not real Git pins.
    removed = []
    cache = SimpleNamespace(
        lock_path=lambda n: env.root / ".locks" / f"{n}.lock",
        operation=lambda *a, **kw: nullcontext(),
        remove_attempt=lambda n, op, *a: removed.append(op),
        collect=lambda n: None,
    )
    maintenance = WorkspaceRetention(env.factory, cache, env.settings)
    assert (await maintenance.sweep()).evicted == 1
    assert f"{1005:032x}" in removed
    async with env.factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(WorkspaceVersion)) == 1006
        )
        assert (await db.get(WorkspaceVersion, versions[-1]["id"])).pin_ref is None
        assert (await db.get(WorkspaceVersion, versions[0]["id"])).pin_ref is not None


@pytest.mark.asyncio
async def test_candidate_rotation_keeps_global_shared_current_and_publishable_protection(
    workspace_env,
):
    env = workspace_env
    view, versions = await seed(env, 1005)
    async with env.factory.begin() as db:
        for i, values in enumerate(versions):
            row = await db.get(WorkspaceVersion, values["id"])
            row.status, row.pin_ref = "ready", f"refs/devflow/pins/{i + 1:032x}"
        # Most versions are current default-branch evidence. The old tag beyond
        # them must eventually be reached, not starved by protection.
        last = await db.get(WorkspaceVersion, versions[-1]["id"])
        last.selection = {"kind": "tag", "name": "old"}
        last.commit_sha = "b" * 40
        # A shared reference outside the candidate page is protected globally.
        first = await db.get(WorkspaceVersion, versions[0]["id"])
        first.selection = {"kind": "tag", "name": "shared"}
        alias = await db.get(WorkspaceVersion, view.id)
        alias.pin_ref = first.pin_ref
        pull_version = await db.get(WorkspaceVersion, versions[2]["id"])
        pull_version.selection = {"kind": "tag", "name": "open-pr"}
        pull_version.commit_sha = "d" * 40
        db.add(
            Fact(
                repository_id=view.repository_id,
                kind="pull",
                source_id=105,
                number=105,
                state="open",
                source_url="https://github.com/team/private/pull/105",
                payload={
                    "title": "P",
                    "state": "open",
                    "issue_id": 105,
                    "base_sha": "d" * 40,
                    "head_sha": "e" * 40,
                    "draft": False,
                    "merged": False,
                },
            )
        )
        attempts = [guard(versions[-1], 2000 + i) for i in range(1001)]
        await db.execute(insert(WorkspaceProcessGuard), attempts)
        job = await db.get(Job, versions[-1]["job_id"])
        job.status, job.lease_owner, job.lease_generation, job.attempt = (
            "running",
            UUID(int=1),
            1,
            1,
        )
        job.updated_at = OLD
    maintenance = WorkspaceRetention(env.factory, None, env.settings)
    async with env.factory() as db:
        anchor = await db.get(WorkspaceVersion, view.id)
    seen = []
    for _ in range(12):
        result = RetentionResult()
        removed, orphans = await maintenance._candidates(anchor, result)
        assert result.examined <= 1000
        assert orphans == []  # drained but still publishable, irrespective of TTL
        seen.extend(identity for identity, _, _ in removed)
    assert versions[-1]["id"] in seen
    assert set(seen) == {versions[-1]["id"]}
    # A newly written row behind the cursor is found after wraparound.
    async with env.factory.begin() as db:
        new_job = Job(
            kind="repository.workspace",
            repository_id=view.repository_id,
            input={},
            input_hash="a" * 64,
            idempotency_key="late-history",
            status="succeeded",
            updated_at=OLD,
        )
        db.add(new_job)
        await db.flush()
        db.add(
            WorkspaceVersion(
                **{
                    **versions[0],
                    "id": UUID(int=0),
                    "job_id": new_job.id,
                    "idempotency_key": "late-history",
                    "selection": {"kind": "tag", "name": "new-old"},
                    "commit_sha": "c" * 40,
                    "status": "ready",
                    "pin_ref": "refs/devflow/pins/late",
                }
            )
        )
        job = await db.get(Job, versions[-1]["job_id"])
        job.status, job.updated_at = "failed", OLD
    seen, orphaned = [], []
    for _ in range(12):
        removed, orphans = await maintenance._candidates(anchor, RetentionResult())
        seen.extend(identity for identity, _, _ in removed)
        orphaned.extend(orphans)
    assert UUID(int=0) in seen
    assert set(orphaned) == {f"{2000 + i:032x}" for i in range(1001)}


def snapshot(pulls=()):
    selection = {
        "selection_order": "test",
        "total_candidates": 0,
        "returned_count": 0,
        "truncated": False,
    }
    return HealthSnapshotView(
        id=uuid4(),
        generation=1,
        generated_at=OLD,
        thresholds={},
        counts={
            "open_issues": None,
            "open_pulls": None,
            "unassigned_issues": None,
            "draft_pulls": None,
            "stale_issues": None,
            "stale_pulls": None,
            "activity": [],
            "actions_categories": None,
        },
        coverage=[],
        manifest={
            "frozen_at": OLD,
            "sync_generation": 0,
            "requested_sequence": 0,
            "processed_sequence": 0,
            "populations": [],
            "coverage": [],
            "pulls": list(pulls),
            "actions": [],
            "pulls_selection": selection,
            "actions_selection": selection,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["busy", "unknown", "failure", "deadline", "changed"]
)
async def test_health_unknown_reason_distinguishes_unavailable_and_budget(
    workspace_env, monkeypatch, outcome
):
    import time

    from devflow.workspace import retention
    from devflow.workspace.process import WorkspaceProcessError

    env = workspace_env
    view = await request(env)
    async with env.factory.begin() as db:
        row = await db.get(WorkspaceVersion, view.id)
        row.status, row.pin_ref = "ready", f"refs/devflow/pins/{uuid4().hex}"
    cache = SimpleNamespace(
        lock_path=lambda n: env.root / ".locks" / f"{n}.lock",
        operation=lambda *a, **kw: nullcontext(),
        inspect=lambda *a: "unknown",
    )
    maintenance = WorkspaceRetention(env.factory, cache, env.settings)
    held = None
    if outcome == "busy":
        held = await reserve_guard(
            env.factory,
            env.settings,
            view.id,
            kind="read",
            actor=env.actor,
            deadline=time.monotonic() + 5,
            lock_path=cache.lock_path(101),
        )
    if outcome == "failure":

        def fail(*args):
            raise WorkspaceProcessError("git_failed")

        cache.inspect = fail
    if outcome == "deadline":

        async def timeout(*args, **kwargs):
            raise TimeoutError

        monkeypatch.setattr(retention, "owned_thread", timeout)
    try:
        observed = await maintenance.health_preflight(
            snapshot(), view.repository_id, "main"
        )
        if outcome == "changed":
            async with env.factory.begin() as db:
                (
                    await db.get(WorkspaceVersion, view.id)
                ).pin_ref = "refs/devflow/pins/changed"
        async with env.factory() as db:
            result = await maintenance.health_evidence(db, snapshot(), "main", observed)
            assert (await db.get(WorkspaceVersion, view.id)).status == "ready"
        entry = result.manifest.workspace_versions[0]
        assert entry.availability == "unknown" and entry.checked_at is None
        assert entry.reason == (
            "verification_limit"
            if outcome == "deadline"
            else "version_changed"
            if outcome == "changed"
            else "verification_unavailable"
        )
    finally:
        if held:
            await revoke_reservation(env.factory, held)


@pytest.mark.asyncio
async def test_sweep_keeps_old_pin_while_reader_has_no_physical_proof(workspace_env):
    import time

    env = workspace_env
    _view, versions = await seed(env, 1)
    async with env.factory.begin() as db:
        row = await db.get(WorkspaceVersion, versions[0]["id"])
        row.status, row.pin_ref = "ready", f"refs/devflow/pins/{uuid4().hex}"
        row.selection = {"kind": "tag", "name": "old"}
    cache = SimpleNamespace(lock_path=lambda n: env.root / ".locks" / f"{n}.lock")
    held = await reserve_guard(
        env.factory,
        env.settings,
        versions[0]["id"],
        kind="read",
        actor=env.actor,
        deadline=time.monotonic() + 5,
        lock_path=cache.lock_path(101),
    )
    try:
        async with env.factory.begin() as db:
            (await db.get(WorkspaceVersion, versions[0]["id"])).last_used_at = OLD
            # Synthetic unresolved guard has no process: do not confuse expiry
            # with evidence. Its future expiry isolates the reader admission fence.
            (
                await db.get(WorkspaceProcessGuard, held.operation_id)
            ).expires_at = datetime(2100, 1, 1, tzinfo=UTC)
        result = await WorkspaceRetention(env.factory, cache, env.settings).sweep()
        assert (result.evicted, result.deferred) == (0, 1)
        async with env.factory() as db:
            assert (
                await db.get(WorkspaceVersion, versions[0]["id"])
            ).pin_ref is not None
    finally:
        await revoke_reservation(env.factory, held)


@pytest.mark.asyncio
async def test_orphans_need_physical_proof_and_old_revocation_even_outside_version_page(
    workspace_env,
):
    env = workspace_env
    view, versions = await seed(env, 4)
    rows = [guard(v, 4000 + i) for i, v in enumerate(versions)]
    rows[0]["proof"] = "never_activated"
    rows[1]["drained_at"] = None
    async with env.factory.begin() as db:
        await db.execute(insert(WorkspaceProcessGuard), rows)
        (await db.get(Job, versions[2]["job_id"])).updated_at = datetime(
            2100, 1, 1, tzinfo=UTC
        )
    async with env.factory() as db:
        anchor = await db.get(WorkspaceVersion, view.id)
    removed, orphans = await WorkspaceRetention(
        env.factory, None, env.settings
    )._candidates(anchor, RetentionResult())
    assert removed == []
    assert orphans == [f"{4003:032x}"]


@pytest.mark.asyncio
async def test_health_pin_budget_retains_verification_limit(workspace_env):
    env = workspace_env
    view, versions = await seed(env, 18)
    async with env.factory.begin() as db:
        for index, values in enumerate(versions):
            row = await db.get(WorkspaceVersion, values["id"])
            row.status, row.pin_ref = "ready", f"refs/devflow/pins/{index:032x}"
            row.selection = {
                "kind": "pull",
                "number": index // 2 + 1,
                "side": "head" if index % 2 else "base",
            }
    frozen = snapshot(
        [
            PRVersion(
                source_id=n,
                number=n,
                base_sha="a" * 40,
                head_sha="a" * 40,
                source_url=None,
                observed_at=OLD,
            )
            for n in range(1, 10)
        ]
    )
    cache = SimpleNamespace(
        lock_path=lambda n: env.root / ".locks" / f"{n}.lock",
        operation=lambda *a, **kw: nullcontext(),
        inspect=lambda *a: "available",
    )
    maintenance = WorkspaceRetention(env.factory, cache, env.settings)
    prepared = await maintenance.health_preflight(frozen, view.repository_id, None)
    async with env.factory() as db:
        result = await maintenance.health_evidence(db, frozen, None, prepared)
    assert result.manifest.workspace_selection.checked_pins == 16
    assert [entry.availability for entry in result.manifest.workspace_versions] == [
        "available"
    ] * 16 + ["unknown"] * 2
    assert [entry.reason for entry in result.manifest.workspace_versions[-2:]] == [
        "verification_limit"
    ] * 2


@pytest.mark.asyncio
async def test_health_candidate_history_cap_retains_verification_limit(workspace_env):
    env = workspace_env
    view, _ = await seed(env, 1001)
    maintenance = WorkspaceRetention(env.factory, None, env.settings)
    prepared = await maintenance.health_preflight(
        snapshot(), view.repository_id, "absent"
    )
    async with env.factory() as db:
        result = await maintenance.health_evidence(db, snapshot(), "absent", prepared)
    entry = result.manifest.workspace_versions[0]
    assert (entry.availability, entry.reason) == ("unknown", "verification_limit")
    assert result.manifest.workspace_selection.checked_pins == 0
