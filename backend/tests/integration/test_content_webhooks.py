import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.github.models import GitHubInstallation, WebhookDelivery
from devflow.github.webhooks import WebhookService
from devflow.jobs.models import Job
from devflow.repositories.models import Repository
from devflow.sync.models import RepositorySync, SyncDemand

pytestmark = pytest.mark.asyncio
SECRET = b"test-content-secret"


async def deliver(env, event="issues", delivery=None, **fields):
    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 11},
            "repository": {"id": 101},
            "issue": {"id": 501, "number": 1},
            **fields,
        }
    ).encode()
    signature = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    await WebhookService(env.factory, SECRET).handle_webhook(
        body, signature, delivery or str(uuid4()), event
    )


async def test_duplicate_delivery_enqueues_once(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    identity = str(uuid4())
    await asyncio.gather(
        deliver(env, delivery=identity), deliver(env, delivery=identity)
    )
    async with env.factory() as db:
        state = await db.get(RepositorySync, repo.id)
        assert state.requested_sequence == 2
        assert await db.scalar(select(func.count()).select_from(Job)) == 1
        assert await db.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
        demand = await db.get(SyncDemand, (repo.id, 2))
        assert demand.target == {
            "kind": "issue",
            "source_id": 501,
            "number": 1,
            "attempt": None,
        }
        assert (await db.scalar(select(GitHubInstallation))).auth_version == 1
        assert (await db.get(Repository, repo.id)).auth_version == 1


async def test_receipt_and_demand_rollback_together(repo_env, monkeypatch):
    from devflow.github import webhooks

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)

    async def fail(*args):
        raise RuntimeError("test audit failure")

    monkeypatch.setattr(webhooks, "write_audit", fail)
    with pytest.raises(RuntimeError):
        await deliver(env)
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
        assert (await db.get(RepositorySync, repo.id)).requested_sequence == 1
        assert await db.scalar(select(func.count()).select_from(SyncDemand)) == 1


async def test_content_for_unregistered_repo_does_not_register(repo_env):
    await deliver(repo_env)
    async with repo_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Repository)) == 0
        assert (
            await db.scalar(select(func.count()).select_from(GitHubInstallation)) == 0
        )
        assert await db.scalar(select(func.count()).select_from(SyncDemand)) == 0
        assert (await db.scalar(select(WebhookDelivery))).status == "ignored"


async def test_removal_blocks_ongoing_content_publication(repo_env):
    from devflow.jobs.queue import Queue
    from devflow.sync.service import SyncService

    env = repo_env
    await env.service.register(env.actor, 11, 101)
    lease = await Queue(env.factory, settings=env.settings).claim(uuid4())
    sync = SyncService(env.factory, env.settings)
    work = await sync.snapshot(lease)
    await deliver(
        env,
        event="installation_repositories",
        action="removed",
        repositories_removed=[{"id": 101}],
    )
    assert not await sync.commit_page(
        lease,
        work,
        facts=[],
        collections=[],
        checkpoint=work.cursor.model_copy(update={"page": 2}),
        complete=False,
    )


@pytest.mark.parametrize(
    "event,fields,target",
    [
        (
            "issue_comment",
            {
                "action": "created",
                "issue": {"id": 999, "number": 3, "pull_request": {}},
            },
            {"kind": "pull", "source_id": None, "number": 3, "attempt": None},
        ),
        (
            "pull_request",
            {"pull_request": {"id": 701, "number": 3}},
            {"kind": "pull", "source_id": 701, "number": 3, "attempt": None},
        ),
        (
            "pull_request_review",
            {"action": "submitted", "pull_request": {"id": 701, "number": 3}},
            {"kind": "pull", "source_id": 701, "number": 3, "attempt": None},
        ),
        (
            "pull_request_review_comment",
            {"action": "deleted", "pull_request": {"id": 701, "number": 3}},
            {"kind": "pull", "source_id": 701, "number": 3, "attempt": None},
        ),
        (
            "workflow_run",
            {"action": "completed", "workflow_run": {"id": 801, "run_attempt": 2}},
            {"kind": "ci_run", "source_id": 801, "number": None, "attempt": 2},
        ),
        (
            "workflow_job",
            {
                "action": "completed",
                "workflow_job": {"id": 901, "run_id": 801, "run_attempt": 2},
            },
            {"kind": "ci_run", "source_id": 801, "number": None, "attempt": 2},
        ),
    ],
)
async def test_finite_content_targets(repo_env, event, fields, target):
    repo = await repo_env.service.register(repo_env.actor, 11, 101)
    await deliver(repo_env, event, **fields)
    async with repo_env.factory() as db:
        assert (await db.get(SyncDemand, (repo.id, 2))).target == target


async def test_version_hints_and_rename_require_reverification(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    for event in ["push", "create", "delete"]:
        await deliver(env, event, action=None, ref="private-ref-do-not-store")
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).version_hint_sequence == 3
    await deliver(env, "repository", action="renamed")
    async with env.factory() as db:
        row = await db.get(Repository, repo.id)
        assert row.needs_verification and row.auth_version == 2
        assert row.full_name == "team/private"
        assert (await db.scalar(select(GitHubInstallation))).auth_version == 1
    from devflow.errors import AppError

    with pytest.raises(AppError):
        await env.policy.require(env.actor, repo.id)


async def test_registration_initial_demand_only_on_change(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    await env.service.register(env.actor, 11, 101)
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).requested_sequence == 1


@pytest.mark.parametrize("running", [False, True])
async def test_reverified_binding_supersedes_old_round_without_losing_demand(
    repo_env, running
):
    from devflow.jobs.queue import Queue
    from devflow.sync.service import SyncService

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    queue = Queue(env.factory, settings=env.settings)
    lease = await queue.claim(uuid4()) if running else None
    sync = SyncService(env.factory, env.settings)
    work = await sync.snapshot(lease) if lease else None
    await deliver(env)
    async with env.factory() as db:
        old_job = (await db.get(RepositorySync, repo.id)).active_job_id
    await deliver(env, "repository", action="renamed")
    await env.service.register(env.actor, 11, 101)
    async with env.factory() as db:
        state = await db.get(RepositorySync, repo.id)
        assert state.generation == 2 and state.requested_sequence == 3
        assert state.active_job_id != old_job
        assert (await db.get(Job, old_job)).status == "failed"
        assert [
            d.sequence
            for d in await db.scalars(select(SyncDemand).order_by(SyncDemand.sequence))
        ] == [1, 2, 3]
    if lease:
        assert not await sync.commit_page(
            lease,
            work,
            facts=[],
            collections=[],
            checkpoint=work.cursor.model_copy(update={"page": 2}),
            complete=False,
        )
    fresh = await queue.claim(uuid4())
    assert fresh is not None
    assert (await sync.snapshot(fresh)).scope.repository_version == 2


async def test_real_worker_uses_injected_transport_for_token_and_content(repo_env):
    from devflow.worker import run_worker

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(env.settings, stop=stop, github=env.github))
    try:
        async with asyncio.timeout(10):
            while True:
                async with env.factory() as db:
                    rows = list(
                        await db.scalars(
                            select(Job).where(Job.repository_id == repo.id)
                        )
                    )
                    assert not any(row.status == "failed" for row in rows), [
                        (str(row.id), row.kind, row.status, row.error_code)
                        for row in rows
                    ]
                    state = await db.get(RepositorySync, repo.id)
                    if state.processed_sequence == state.requested_sequence:
                        assert state.status == "completed"
                        break
                await asyncio.sleep(0.05)
        assert any(
            req.method == "POST" and req.url.path.endswith("/access_tokens")
            for req in env.calls
        )
        assert any(
            req.method == "GET" and req.url.path.startswith("/repos/")
            for req in env.calls
        )
    finally:
        stop.set()
        await asyncio.wait_for(worker, 5)


@pytest.mark.parametrize("action", ["renamed", "transferred", "edited"])
async def test_late_metadata_hint_cannot_soften_repository_removal(repo_env, action):
    from devflow.errors import AppError

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    await deliver(
        env,
        "installation_repositories",
        action="removed",
        repositories_removed=[{"id": 101}],
    )
    await deliver(env, "repository", action=action)
    async with env.factory() as db:
        row = await db.get(Repository, repo.id)
        assert not row.available and not row.needs_verification
        assert (await db.get(RepositorySync, repo.id)).requested_sequence == 1
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.status == 404


@pytest.mark.parametrize(
    "fields",
    [
        {"installation": {"id": True}},
        {"repository": {"id": "101"}},
        {"issue": {"id": 501, "number": 0}},
        {"issue": None},
        {"issue": {"id": 2**63, "number": 1}},
        {"action": []},
    ],
)
async def test_content_parser_rejects_invalid_identity_without_receipt(
    repo_env, fields
):
    from devflow.errors import AppError

    with pytest.raises(AppError) as error:
        await deliver(repo_env, **fields)
    assert error.value.status == 400
    async with repo_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


async def test_rebinding_rebuilds_retained_backfill_checkpoint(repo_env):
    from datetime import UTC, datetime

    from support.sync_env import claim_fact_sync

    from devflow.jobs.queue import Queue
    from devflow.sync.scheduler import request_sync
    from devflow.sync.schemas import BackfillRange
    from devflow.sync.service import SyncService

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    queue, sync = (
        Queue(env.factory, settings=env.settings),
        SyncService(env.factory, env.settings),
    )
    initial = await queue.claim(uuid4())
    for _ in range(3):
        work = await sync.snapshot(initial)
        assert await sync.commit_page(
            initial,
            work,
            facts=[],
            collections=[],
            checkpoint=work.cursor.model_copy(update={"stage": "done"}),
            complete=True,
        )
    window = BackfillRange(
        since=datetime(2024, 1, 1, tzinfo=UTC),
        until=datetime(2024, 1, 2, tzinfo=UTC),
        streams=["issues"],
    )
    async with env.factory.begin() as db:
        await request_sync(db, repo.id, reason="backfill", backfill=window)
    old = await claim_fact_sync(queue, env.factory, repo.id)
    work = await sync.snapshot(old)
    assert await sync.commit_page(
        old,
        work,
        facts=[],
        collections=[],
        checkpoint=work.cursor.model_copy(update={"page": 2}),
        complete=False,
    )
    await deliver(env, "repository", action="renamed")
    await env.service.register(env.actor, 11, 101)
    fresh = await queue.claim(uuid4())
    current = await sync.snapshot(fresh)
    assert current.cursor.page == 1 and current.cursor.mode == "backfill"
    assert (
        current.cursor.window.since == window.since
        and current.cursor.window.until == window.until
    )
    assert current.captured_sequence == work.captured_sequence == 2
    assert current.generation > work.generation
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).processed_sequence == 1
        assert [
            d.sequence
            for d in await db.scalars(select(SyncDemand).order_by(SyncDemand.sequence))
        ] == [2, 3]
