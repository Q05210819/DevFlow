"""Offline GitHub boundary; actual OAuth, API, scheduler, Worker and PostgreSQL."""

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from support.full_flow import (
    ADMIN_ID,
    INSTALLATION_ID,
    MEMBER_ID,
    REPOSITORY_ID,
    SyntheticGitHub,
    csrf_headers,
    oauth_login,
    synthetic_settings,
)

from devflow.app import create_app
from devflow.bootstrap import bootstrap_admin
from devflow.facts.models import Fact
from devflow.github.client import GitHubClient
from devflow.github.models import WebhookDelivery
from devflow.jobs.models import Job
from devflow.sync.models import RepositorySync, SyncCursor
from devflow.worker import run_worker

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def data_flow(migrated_database, tmp_path):
    settings = synthetic_settings(
        tmp_path,
        migrated_database[0].database_url.get_secret_value(),
        "http://localhost:3000",
    ).model_copy(update={"job_retry_first_seconds": 1})
    remote = SyntheticGitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as provider:
        github = GitHubClient(provider, settings)
        app = create_app(settings, github=github)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.public_base_url,
            ) as admin,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.public_base_url,
            ) as member,
        ):
            await bootstrap_admin(app.state.session_factory, ADMIN_ID)
            admin_session = await oauth_login(admin, "admin")
            headers = csrf_headers(admin_session, settings.public_base_url)
            assert (
                await admin.post(
                    "/api/v1/admin/users",
                    headers=headers,
                    json={"github_id": MEMBER_ID, "role": "member"},
                )
            ).status_code == 201
            member_session = await oauth_login(member, "member")
            discovery = await admin.get("/api/v1/github/repositories")
            assert discovery.json()["items"][0]["github_repository_id"] == REPOSITORY_ID
            registered = await admin.post(
                "/api/v1/repositories",
                headers=headers,
                json={
                    "installation_id": INSTALLATION_ID,
                    "github_repository_id": REPOSITORY_ID,
                },
            )
            assert registered.status_code == 201, registered.text
            yield SimpleNamespace(
                settings=settings,
                remote=remote,
                github=github,
                admin=admin,
                member=member,
                factory=app.state.session_factory,
                repository_id=UUID(registered.json()["id"]),
                path=f"/api/v1/repositories/{registered.json()['id']}",
                headers=csrf_headers(member_session, settings.public_base_url),
            )


@asynccontextmanager
async def worker(env):
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(env.settings, stop=stop, github=env.github))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 35)


async def webhook(env, event="issues", delivery=None, **fields):
    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": INSTALLATION_ID},
            "repository": {"id": REPOSITORY_ID},
            "issue": {"id": 51001, "number": 1},
            **fields,
        }
    ).encode()
    signature = hmac.new(b"synthetic-acceptance-webhook-secret", body, hashlib.sha256)
    response = await env.admin.post(
        "/api/v1/github/webhooks",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=" + signature.hexdigest(),
            "X-GitHub-Delivery": delivery or str(uuid4()),
            "X-GitHub-Event": event,
        },
    )
    assert response.status_code == 204, response.text


async def wait_state(env, predicate):
    async with asyncio.timeout(45):
        while True:
            async with env.factory() as db:
                state = await db.get(RepositorySync, env.repository_id)
                jobs = list(
                    await db.scalars(
                        select(Job).where(Job.repository_id == env.repository_id)
                    )
                )
                assert not any(j.status == "failed" for j in jobs), [
                    (str(j.id), j.kind, j.status, j.error_code, j.request_count)
                    for j in jobs
                ]
                if predicate(state):
                    return state
            await asyncio.sleep(0.05)


async def completed(env):
    return await wait_state(
        env,
        lambda s: (
            s.status == "completed"
            and s.processed_sequence == s.requested_sequence
            and s.health_status == "completed"
            and s.health_processed_sequence == s.health_requested_sequence
        ),
    )


async def test_registration_webhook_facts_health_permission_gap_and_revocation(
    data_flow,
):
    env = data_flow
    delivery = str(uuid4())
    await webhook(env, delivery=delivery)
    await webhook(env, delivery=delivery)
    async with env.factory() as db:
        assert (await db.get(RepositorySync, env.repository_id)).requested_sequence == 2
        assert len(list(await db.scalars(select(WebhookDelivery)))) == 1
    async with worker(env):
        await completed(env)
        issues = (await env.member.get(env.path + "/issues")).json()
        assert {i["source_id"] for i in issues["items"]} == {51001, 51003}
        assert issues["coverage"]["status"] == "complete"
        pull = (await env.member.get(env.path + "/pulls/2")).json()["item"]
        assert (pull["source_id"], pull["payload"]["issue_id"]) == (71002, 51002)
        files = (
            await env.member.get(env.path + "/pulls/2", params={"collection": "files"})
        ).json()
        assert files["collection"]["complete"] is True
        assert files["items"][0]["payload"]["patch_status"] == "missing"
        runs = (await env.member.get(env.path + "/actions/runs")).json()["items"]
        assert [
            (r["source_id"], r["payload"]["attempt"], r["payload"]["conclusion"])
            for r in runs
        ] == [(81001, 2, "success")]
        old = (
            await env.member.get(
                env.path + "/actions/runs/81001/attempts/1",
                params={"collection": "jobs"},
            )
        ).json()
        assert old["items"][0]["payload"]["conclusion"] == "failure"
        health = (await env.member.get(env.path + "/health")).json()["snapshot"]
        assert health["counts"]["open_issues"] == 2
        assert health["counts"]["open_pulls"] == 1
        assert health["workspace"] == "unavailable"
        assert health["manifest"]["actions"][0]["pr_numbers"] == [2]
        async with env.factory() as db:
            cursor = await db.get(SyncCursor, (env.repository_id, "actions"))
            watermark = cursor.completed_watermark
            assert watermark is not None
        env.remote.actions_permission_missing = True
        refreshed = await env.member.post(
            env.path + "/sync/refresh",
            json={},
            headers={**env.headers, "Idempotency-Key": str(uuid4())},
        )
        assert refreshed.status_code == 202, refreshed.text
        await completed(env)
        sync = (await env.member.get(env.path + "/sync")).json()
        actions = next(s for s in sync["streams"] if s["stream"] == "actions")
        assert actions["status"] == "permission_missing"
        assert actions["error_code"] == "permission_missing"
        async with env.factory() as db:
            assert (
                await db.get(SyncCursor, (env.repository_id, "actions"))
            ).completed_watermark == watermark
            assert (
                len(list(await db.scalars(select(Fact).where(Fact.kind == "ci_run"))))
                == 2
            )
        health = (await env.member.get(env.path + "/health")).json()["snapshot"]
        assert (
            next(c for c in health["coverage"] if c["stream"] == "actions")["status"]
            == "permission_missing"
        )
        await webhook(
            env,
            "installation_repositories",
            action="removed",
            repositories_removed=[{"id": REPOSITORY_ID}],
        )
        for suffix in ("/issues", "/pulls/2", "/actions/runs", "/sync", "/health"):
            denied = await env.member.get(env.path + suffix)
            assert denied.status_code == 404, denied.text
            assert denied.json()["error"]["code"] == "resource_not_found"
            assert "Private" not in denied.text


async def test_rate_limit_checkpoint_and_new_demand_survive_normal_worker_restart(
    data_flow,
):
    env = data_flow
    env.remote.rate_limit_page2 = True
    async with worker(env):
        await wait_state(env, lambda s: s.status == "retry_wait")
        async with env.factory() as db:
            cursor = await db.get(SyncCursor, (env.repository_id, "issues"))
            assert cursor.checkpoint["page"] == 2
            assert cursor.completed_watermark is None
            state = await db.get(RepositorySync, env.repository_id)
            job = await db.get(Job, state.active_job_id)
            assert job.error_code == "github_rate_limited"
            assert job.available_at > job.attempt_started_at
            assert state.processed_sequence == 0
    await webhook(env)
    async with env.factory() as db:
        state = await db.get(RepositorySync, env.repository_id)
        assert (state.requested_sequence, state.processed_sequence) == (2, 0)
    async with worker(env):
        await completed(env)
    async with env.factory() as db:
        facts = list(await db.scalars(select(Fact).where(Fact.kind == "issue")))
        assert sorted(f.source_id for f in facts) == [51001, 51003]
        cursors = list(await db.scalars(select(SyncCursor)))
        assert all(c.completed_watermark is not None for c in cursors)
        jobs = list(await db.scalars(select(Job).where(Job.kind == "repository.sync")))
        assert len(jobs) > 1
        assert all(j.request_count <= 5 for j in jobs)
        assert any(j.attempt == 2 for j in jobs)
