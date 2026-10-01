import asyncio
import hashlib
import json

import pytest
from sqlalchemy import func, select, update
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.job_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import browser, repository

from devflow.audit.models import AuditLog
from devflow.errors import AppError

pytestmark = pytest.mark.asyncio


async def test_concurrent_submission_idempotency(job_env):
    from devflow.jobs.models import Job, RunEvent

    env = job_env
    barrier = asyncio.Barrier(2)

    async def submit():
        await barrier.wait()
        return await env.jobs.submit_probe(env.actor, env.repository.id, "same-key")

    first, second = await asyncio.gather(submit(), submit())
    assert first.id == second.id
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "job.submitted")
            )
            == 1
        )
        job = await session.get(Job, first.id)
        assert job.input == {"repository_id": str(env.repository.id)}
        assert (
            job.input_hash
            == hashlib.sha256(
                json.dumps(job.input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        assert job.attempt == job.lease_generation == 0
        assert job.max_attempts == 3 and job.last_sequence == 1
        assert job.lease_owner is job.lease_expires_at is None
        event = await session.scalar(select(RunEvent))
        assert event.event == "job.status"
        assert event.sequence == 1
        assert event.payload == {"status": "queued", "attempt": 0, "error_code": None}


async def test_factory_persists_configured_lower_attempt_budget(job_env):
    from uuid import UUID

    from devflow.jobs.models import Job

    env = job_env
    env.settings = env.settings.model_copy(update={"job_max_attempts": 2})
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        current = (await client.get("/api/v1/session")).json()
        response = await client.post(
            f"/api/v1/repositories/{env.repository.id}/access-checks",
            headers={
                "Origin": env.settings.public_base_url,
                "X-CSRF-Token": current["csrf_token"],
                "Idempotency-Key": "budget",
            },
        )
        assert response.status_code == 202
        assert response.json()["max_attempts"] == 2
    async with env.factory() as session:
        assert (await session.get(Job, UUID(response.json()["id"]))).max_attempts == 2


async def test_same_key_different_input_conflicts_without_mutating_original(job_env):
    from devflow.jobs.models import Job

    env = job_env
    env.repositories[(11, 1)] = ([repository(), repository(102)], None)
    other = await env.service.register(env.actor, 11, 102)
    original = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    with pytest.raises(AppError) as error:
        await env.jobs.submit_probe(env.actor, other.id, "key")
    assert error.value.status == 409 and error.value.code == "idempotency_conflict"
    async with env.factory() as session:
        row = await session.get(Job, original.id)
        assert row.repository_id == env.repository.id
        assert row.input == {"repository_id": str(env.repository.id)}
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_idempotency_does_not_bypass_reauthorization(job_env):
    env = job_env
    await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    env.repositories = {}
    with pytest.raises(AppError) as error:
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    assert error.value.status == 404


async def test_same_key_is_independent_for_each_creator(job_env):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.models import GitHubCredential

    env = job_env
    first = await env.jobs.submit_probe(env.actor, env.repository.id, "shared-key")
    async with env.factory.begin() as session:
        user = User(github_id=43, role="member")
        session.add(user)
        await session.flush()
        session.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=env.cipher.encrypt(
                    SecretStr("other-user-token")
                ),
            )
        )
        other = Actor(user.id, user.github_id, user.role, user.auth_version)
    second = await env.jobs.submit_probe(other, env.repository.id, "shared-key")
    assert first.id != second.id
    assert second.requested_by == other.id
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(env.actor, second.id)
    assert error.value.status == 404


async def test_transient_reauthorization_failure_never_returns_existing_job(job_env):
    env = job_env
    await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    env.status = 503
    with pytest.raises(AppError) as error:
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    assert error.value.status == 503


async def test_concurrent_http_requests_return_one_committed_job(job_env):
    from devflow.jobs.models import Job, RunEvent

    env = job_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        current = (await client.get("/api/v1/session")).json()
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": current["csrf_token"],
            "Idempotency-Key": "concurrent-http",
        }
        barrier = asyncio.Barrier(2)

        async def submit():
            await barrier.wait()
            return await client.post(
                f"/api/v1/repositories/{env.repository.id}/access-checks",
                headers=headers,
            )

        first, second = await asyncio.gather(submit(), submit())
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        assert await session.scalar(select(func.count()).select_from(RunEvent)) == 1


async def test_idempotency_unique_scope_is_enforced_by_database(job_env):
    from sqlalchemy.exc import IntegrityError

    from devflow.jobs.models import Job

    env = job_env
    submitted = await env.jobs.submit_probe(env.actor, env.repository.id, "unique-db")
    async with env.factory() as session:
        existing = await session.get(Job, submitted.id)
        values = {
            name: getattr(existing, name)
            for name in (
                "repository_id",
                "requested_by",
                "kind",
                "idempotency_key",
                "input",
                "input_hash",
            )
        }
    with pytest.raises(IntegrityError):
        async with env.factory.begin() as session:
            session.add(Job(**values))
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_submit_rechecks_local_authorization_after_remote_verification(job_env):
    from devflow.repositories.models import Repository

    env = job_env

    async def revoke(request):
        if request.url.path.endswith("/repositories"):
            async with env.factory.begin() as session:
                await session.execute(
                    update(Repository).values(
                        available=False,
                        needs_verification=False,
                        auth_version=Repository.auth_version + 1,
                    )
                )

    env.before_response = revoke
    with pytest.raises(AppError) as error:
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    assert error.value.status == 404


@pytest.mark.parametrize("replay", [False, True])
async def test_submission_fences_epoch_change_after_policy_returns(
    job_env, monkeypatch, replay
):
    from devflow.github.models import GitHubInstallation
    from devflow.jobs.models import Job

    env = job_env
    if replay:
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    require = env.policy.require

    async def changed_after_verification(*args, **kwargs):
        view = await require(*args, **kwargs)
        async with env.factory.begin() as session:
            await session.execute(
                update(GitHubInstallation).values(
                    auth_version=GitHubInstallation.auth_version + 1
                )
            )
        return view

    monkeypatch.setattr(env.policy, "require", changed_after_verification)
    with pytest.raises(AppError) as error:
        await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    assert error.value.status == 404
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == int(
            replay
        )


async def test_real_factory_routes_persist_before_202_and_hide_internal_fields(job_env):
    from devflow.jobs.models import Job

    env = job_env
    path = f"/api/v1/repositories/{env.repository.id}/access-checks"
    async with browser(env) as (client, app, raw):
        assert app.state.job_service.factory is app.state.session_factory
        assert app.state.job_service.policy is app.state.repository_policy
        assert (
            await client.post(path, headers={"Idempotency-Key": "key"})
        ).status_code == 401
        client.cookies.set("devflow_session", raw)
        session = (await client.get("/api/v1/session")).json()
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": session["csrf_token"],
            "Idempotency-Key": "key",
        }
        assert (
            await client.post(path, headers={"Idempotency-Key": "key"})
        ).status_code == 403
        response = await client.post(path, headers=headers)
        assert response.status_code == 202
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert set(result) == {
            "id",
            "repository_id",
            "requested_by",
            "status",
            "attempt",
            "max_attempts",
            "result",
            "error_code",
            "last_sequence",
            "created_at",
            "updated_at",
        }
        assert result["status"] == "queued" and result["last_sequence"] == 1
        async with env.factory() as db:
            from uuid import UUID

            assert await db.get(Job, UUID(result["id"])) is not None
        again = await client.post(path, headers=headers)
        assert again.status_code == 202 and again.json()["id"] == result["id"]
        detail = await client.get(f"/api/v1/jobs/{result['id']}")
        assert detail.status_code == 200 and detail.json() == result


@pytest.mark.parametrize("key", [None, "", " ", "two words", "x" * 129])
async def test_http_requires_valid_idempotency_key(job_env, key):
    env = job_env
    async with browser(env) as (client, _, raw):
        client.cookies.set("devflow_session", raw)
        session = (await client.get("/api/v1/session")).json()
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": session["csrf_token"],
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        response = await client.post(
            f"/api/v1/repositories/{env.repository.id}/access-checks", headers=headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
