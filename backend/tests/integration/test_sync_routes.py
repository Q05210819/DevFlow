from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from support.repository_env import browser
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.jobs.models import Job
from devflow.sync.models import RepositorySync, SyncDemand

pytestmark = pytest.mark.asyncio


def headers(env, app, raw, key="refresh-key"):
    return {
        "Origin": env.settings.public_base_url,
        "X-CSRF-Token": app.state.auth_service.session_csrf(raw),
        "Idempotency-Key": key,
    }


async def test_sync_controls_csrf_idempotency_and_durable_receipt(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    path = f"/api/v1/repositories/{repo.id}/sync"
    async with browser(env) as (client, app, raw):
        assert (await client.get(path)).status_code == 401
        client.cookies.set("devflow_session", raw)
        view = await client.get(path)
        assert view.status_code == 200
        assert view.json()["requested_sequence"] == 1
        assert len(view.json()["streams"]) == 3
        assert (await client.post(path + "/refresh", json={})).status_code == 403
        h = headers(env, app, raw)
        assert (
            await client.post(
                path + "/refresh",
                json={},
                headers={k: v for k, v in h.items() if k != "Idempotency-Key"},
            )
        ).status_code == 422
        first = await client.post(path + "/refresh", json={}, headers=h)
        assert first.status_code == 202
        assert first.json()["requested_sequence"] == 2
        async with env.factory.begin() as db:
            await db.execute(
                delete(SyncDemand).where(SyncDemand.repository_id == repo.id)
            )
        repeated = await client.post(
            path + "/refresh", json={"resume": False}, headers=h
        )
        assert repeated.status_code == 202
        async with env.factory() as db:
            assert (await db.get(RepositorySync, repo.id)).requested_sequence == 2
        conflict = await client.post(
            path + "/refresh", json={"resume": True}, headers=h
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert view.headers["cache-control"] == "no-store"


async def test_admin_still_needs_fresh_repository_permission(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, repo.id)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        assert (
            await client.get(f"/api/v1/repositories/{repo.id}/sync")
        ).status_code == 200
        env.repositories = {}
        for endpoint, body in [
            ("refresh", {}),
            ("refresh", {"resume": True}),
            (
                "backfills",
                {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2026-01-02T00:00:00Z",
                    "streams": ["issues"],
                },
            ),
        ]:
            result = await client.post(
                f"/api/v1/repositories/{repo.id}/sync/{endpoint}",
                json=body,
                headers=headers(env, app, raw, endpoint),
            )
            assert result.status_code == 404
    from devflow.audit.models import AuditLog

    async with env.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "permission.denied")
            )
            == 3
        )


@pytest.mark.parametrize(
    "start,end,streams,expected",
    [
        ("2024-01-01T00:00:00Z", "2024-12-31T00:00:00Z", ["issues"], 202),
        ("2024-01-01T00:00:00+08:00", "2024-01-01T01:00:00+08:00", ["pulls"], 202),
        ("2024-01-01T00:00:00", "2024-01-02T00:00:00Z", ["issues"], 422),
        ("2024-01-02T00:00:00Z", "2024-01-01T00:00:00Z", ["issues"], 422),
        ("2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", ["issues"], 422),
        ("2099-01-01T00:00:00Z", "2099-01-02T00:00:00Z", ["actions"], 422),
        ("2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", ["issues", "issues"], 422),
    ],
)
async def test_backfill_utc_and_boundaries(repo_env, start, end, streams, expected):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        result = await client.post(
            f"/api/v1/repositories/{repo.id}/sync/backfills",
            json={"start": start, "end": end, "streams": streams},
            headers=headers(env, app, raw),
        )
        assert result.status_code == expected
    if expected == 202:
        async with env.factory() as db:
            demand = await db.get(SyncDemand, (repo.id, 2))
            assert datetime.fromisoformat(
                demand.backfill["since"]
            ).utcoffset() == timedelta(0)
            assert demand.backfill["streams"] == streams


async def test_member_cannot_backfill_or_resume_but_can_refresh(repo_env):
    from devflow.auth.models import User

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    async with env.factory.begin() as db:
        (await db.get(User, env.actor.id)).role = "member"
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        path = f"/api/v1/repositories/{repo.id}/sync"
        assert (
            await client.post(
                path + "/refresh", json={"resume": True}, headers=headers(env, app, raw)
            )
        ).status_code == 403
        assert (
            await client.post(
                path + "/backfills",
                json={
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-02T00:00:00Z",
                    "streams": ["issues"],
                },
                headers=headers(env, app, raw),
            )
        ).status_code == 403
        assert (
            await client.post(
                path + "/refresh", json={}, headers=headers(env, app, raw)
            )
        ).status_code == 202


async def test_sync_view_retry_time_is_persisted_and_safe(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    async with env.factory.begin() as db:
        state = await db.get(RepositorySync, repo.id)
        state.status = "retry_wait"
        job = await db.get(Job, state.active_job_id)
        job.status = "retry_wait"
        job.error_code = "github_rate_limited"
        job.available_at = await db.scalar(
            select(func.clock_timestamp() + timedelta(seconds=90))
        )
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get(f"/api/v1/repositories/{repo.id}/sync")
        assert response.status_code == 200
        assert response.json()["retry_at"] is not None
        assert response.json()["error_code"] == "github_rate_limited"
        assert int(response.headers["retry-after"]) > 0
        assert "checkpoint" not in response.text and "input_hash" not in response.text


async def test_command_receipt_demand_and_audit_are_atomic(repo_env, monkeypatch):
    from devflow.sync import scheduler
    from devflow.sync.models import RepositoryCommandReceipt

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)

    async def fail(*args):
        raise RuntimeError("audit transaction failure")

    monkeypatch.setattr(scheduler, "write_audit", fail)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.post(
            f"/api/v1/repositories/{repo.id}/sync/refresh",
            json={},
            headers=headers(env, app, raw),
        )
        assert response.status_code == 500
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).requested_sequence == 1
        assert (
            await db.scalar(select(func.count()).select_from(RepositoryCommandReceipt))
            == 0
        )
        assert await db.scalar(select(func.count()).select_from(SyncDemand)) == 1


async def test_backfill_normalized_payload_and_conflict(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        path = f"/api/v1/repositories/{repo.id}/sync/backfills"
        body = {
            "start": "2024-01-01T08:00:00+08:00",
            "end": "2024-01-02T08:00:00+08:00",
            "streams": ["issues", "pulls"],
        }
        h = headers(env, app, raw)
        assert (await client.post(path, json=body, headers=h)).status_code == 202
        body.update(
            start="2024-01-01T00:00:00Z",
            end="2024-01-02T00:00:00Z",
            streams=["pulls", "issues"],
        )
        assert (await client.post(path, json=body, headers=h)).status_code == 202
        body["streams"] = ["actions"]
        assert (await client.post(path, json=body, headers=h)).status_code == 409
    async with env.factory() as db:
        assert (await db.get(RepositorySync, repo.id)).requested_sequence == 2


async def test_only_authorized_explicit_resume_reopens_budget(repo_env):
    from uuid import uuid4

    from test_content_webhooks import deliver

    from devflow.jobs.queue import Queue
    from devflow.sync.service import SyncService

    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    queue = Queue(env.factory, settings=env.settings)
    lease = await queue.claim(uuid4())
    await SyncService(env.factory, env.settings).snapshot(lease)
    await queue.fail(lease, "budget_exhausted", False)
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        path = f"/api/v1/repositories/{repo.id}/sync/refresh"
        ordinary = await client.post(path, json={}, headers=headers(env, app, raw))
        assert ordinary.status_code == 202
        assert ordinary.json()["status"] == "budget_exhausted"
        assert ordinary.json()["error_code"] == "budget_exhausted"
        await deliver(env)
        async with env.factory() as db:
            assert (await db.get(RepositorySync, repo.id)).active_job_id is None
        resumed = await client.post(
            path, json={"resume": True}, headers=headers(env, app, raw, "resume-key")
        )
        assert resumed.status_code == 202 and resumed.json()["status"] == "queued"
        assert resumed.json()["generation"] == ordinary.json()["generation"]


async def test_get_does_not_schedule_or_read_content(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    env.calls.clear()
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get(f"/api/v1/repositories/{repo.id}/sync")
        assert (
            response.status_code == 200 and response.json()["requested_sequence"] == 1
        )
        assert all(
            item["coverage"]
            == {"start": None, "end": None, "completed_watermark": None}
            for item in response.json()["streams"]
        )
    assert all(
        req.method == "GET" and not req.url.path.startswith("/repos/")
        for req in env.calls
    )


async def test_fresh_authorization_rate_limit_has_retry_after(repo_env):
    env = repo_env
    repo = await env.service.register(env.actor, 11, 101)
    env.status = 429
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.post(
            f"/api/v1/repositories/{repo.id}/sync/refresh",
            json={},
            headers=headers(env, app, raw),
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "github_rate_limited"
        assert int(response.headers["retry-after"]) > 0
