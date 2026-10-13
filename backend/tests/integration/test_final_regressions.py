import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.audit.models import AuditLog
from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import Session, User
from devflow.errors import AppError

pytestmark = pytest.mark.asyncio


async def test_logout_disable_overlap_and_duplicate_logout(auth_env, monkeypatch):
    import devflow.auth.service as module

    target = await member(auth_env)
    token = (await login(auth_env)).raw_token.get_secret_value()
    await member(auth_env, github_id=102, role="admin")
    auth_env.github_id = 102
    actor = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    reached, release = asyncio.Event(), asyncio.Event()
    original = module.write_audit
    pid = None

    async def paused(db, entry):
        nonlocal pid
        if entry.action == "auth.logout":
            pid = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return await original(db, entry)

    monkeypatch.setattr(module, "write_audit", paused)
    tasks = [asyncio.create_task(auth_env.service.logout(token))]
    try:
        async with asyncio.timeout(5):
            await reached.wait()
        tasks.append(
            asyncio.create_task(
                auth_env.service.update_member(
                    actor, target.id, role=None, enabled=False
                )
            )
        )
        async with asyncio.timeout(5):
            while True:
                async with auth_env.factory() as db:
                    waiting = await db.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :pid = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"pid": pid},
                    )
                if waiting:
                    break
        release.set()
        async with asyncio.timeout(10):
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert not any(isinstance(result, Exception) for result in results)
        await auth_env.service.logout(token)
        async with auth_env.factory() as db:
            assert not (await db.get(User, target.id)).enabled
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Session)
                    .where(Session.user_id == target.id)
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action == "auth.logout", AuditLog.actor_id == target.id
                    )
                )
                == 1
            )
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("ttl", [0, 5])
async def test_permission_cache_uses_configured_ttl(repo_env, monkeypatch, ttl):
    from devflow.repositories import policy

    env = repo_env
    env.settings.permission_cache_ttl_seconds = ttl
    now = [100.0]
    monkeypatch.setattr(policy.time, "monotonic", lambda: now[0])
    registered = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, registered.id)
    env.status = 503
    if ttl:
        now[0] += ttl - 1
        assert (await env.policy.require(env.actor, registered.id)).id == registered.id
        now[0] += 1
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id)
    assert error.value.status == 503


async def test_http_audit_correlation_and_context_isolation(auth_env, caplog):
    await member(auth_env, role="admin")
    token = (await login(auth_env)).raw_token.get_secret_value()
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", token)
        csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {
            "Origin": "http://localhost:3000",
            "X-CSRF-Token": csrf,
            "X-Request-ID": "untrusted-client-id",
        }
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/admin/users",
                    headers=headers,
                    json={"github_id": value, "role": "member"},
                )
                for value in [201, 202]
            )
        )
        responses.append(
            await client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={"github_id": 201, "role": "member"},
            )
        )
        responses.append(
            await client.post(
                "/api/v1/admin/users", json={"github_id": 203, "role": "member"}
            )
        )
    assert [r.status_code for r in responses] == [201, 201, 409, 403]
    ids = {UUID(r.headers["X-Request-ID"]) for r in responses}
    assert len(ids) == 4
    logs = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "devflow"
    ]
    assert ids <= {UUID(record["request_id"]) for record in logs}
    explicit, job = uuid4(), uuid4()
    async with auth_env.factory.begin() as db:
        await write_audit(
            db,
            AuditEntry(
                action="bootstrap.admin", resource_type="user", outcome="success"
            ),
        )
        await write_audit(
            db,
            AuditEntry(
                action="job.started",
                resource_type="job",
                outcome="success",
                request_id=explicit,
                job_id=job,
            ),
        )
    async with auth_env.factory() as db:
        audits = (
            await db.scalars(
                select(AuditLog).where(
                    AuditLog.action.in_(["member.created", "permission.denied"])
                )
            )
        ).all()
        assert {a.request_id for a in audits} == ids
        for response in responses:
            audit = next(
                a
                for a in audits
                if a.request_id == UUID(response.headers["X-Request-ID"])
            )
            assert audit.outcome == (
                "success" if response.status_code == 201 else "failure"
            )
        assert (
            await db.scalar(
                select(AuditLog.request_id).where(AuditLog.action == "bootstrap.admin")
            )
            is None
        )
        row = await db.scalar(select(AuditLog).where(AuditLog.action == "job.started"))
        assert (row.request_id, row.job_id) == (explicit, job)


async def test_streaming_audits_keep_their_request_context(auth_env):
    from fastapi.responses import StreamingResponse

    from devflow.audit.service import request_id_context

    barrier = asyncio.Barrier(2)
    explicit = uuid4()

    @auth_env.app.get("/test-audit-stream")
    async def stream_route():
        async def body():
            yield "data: start\n\n"
            await barrier.wait()
            async with auth_env.factory.begin() as db:
                await write_audit(
                    db,
                    AuditEntry(
                        action="webhook.verified",
                        resource_type="webhook",
                        outcome="success",
                    ),
                )
                await write_audit(
                    db,
                    AuditEntry(
                        action="webhook.processed",
                        resource_type="webhook",
                        outcome="success",
                        request_id=explicit,
                    ),
                )
            yield "data: end\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    async with browser(auth_env) as client:
        async with asyncio.timeout(5):
            responses = await asyncio.gather(
                client.get("/test-audit-stream"), client.get("/test-audit-stream")
            )
    assert all(r.status_code == 200 and "data: end" in r.text for r in responses)
    assert request_id_context.get() is None
    async with auth_env.factory() as db:
        assert set(
            await db.scalars(
                select(AuditLog.request_id).where(AuditLog.action == "webhook.verified")
            )
        ) == {UUID(r.headers["X-Request-ID"]) for r in responses}
        assert set(
            await db.scalars(
                select(AuditLog.request_id).where(
                    AuditLog.action == "webhook.processed"
                )
            )
        ) == {explicit}


async def test_logout_audit_failure_rolls_back_session_deletion(auth_env, monkeypatch):
    import devflow.auth.service as module

    await member(auth_env)
    raw = (await login(auth_env)).raw_token.get_secret_value()

    async def fail_audit(db, entry):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(module, "write_audit", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        await auth_env.service.logout(raw)
    assert (await auth_env.service.resolve_session(raw)).github_id == 101
