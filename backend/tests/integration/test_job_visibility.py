from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.job_env import repo_env as repo_env  # noqa: PLC0414

from devflow.auth.models import User
from devflow.auth.types import Actor
from devflow.errors import AppError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("role", ["admin", "member"])
async def test_only_creator_can_read_job(job_env, role):
    from pydantic import SecretStr

    from devflow.github.models import GitHubCredential

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory.begin() as session:
        user = User(github_id=43, role=role)
        session.add(user)
        await session.flush()
        other = Actor(user.id, user.github_id, role, user.auth_version)
        if role == "member":
            session.add(
                GitHubCredential(
                    user_id=user.id,
                    access_token_encrypted=env.cipher.encrypt(
                        SecretStr("other-user-token")
                    ),
                )
            )
    if role == "member":
        await env.policy.require(other, env.repository.id)
    env.calls.clear()
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(other, job.id)
    assert error.value.status == 404
    assert env.calls == []


async def test_creator_requires_current_repository_access(job_env):
    from devflow.repositories.models import Repository

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository).values(
                enabled=False, auth_version=Repository.auth_version + 1
            )
        )
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(env.actor, job.id)
    assert error.value.status == 404


async def test_disabled_creator_cannot_read_with_old_actor(job_env):
    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory.begin() as session:
        await session.execute(
            update(User)
            .where(User.id == env.actor.id)
            .values(enabled=False, auth_version=User.auth_version + 1)
        )
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(env.actor, job.id)
    assert error.value.status == 401


async def test_unknown_job_has_same_safe_error(job_env):
    env = job_env
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(env.actor, uuid4())
    assert error.value.status == 404 and error.value.code == "resource_not_found"


async def test_creator_can_read_persisted_safe_result(job_env):
    from devflow.jobs.models import Job

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    async with env.factory.begin() as session:
        row = await session.scalar(
            select(Job).where(Job.id == job.id).with_for_update()
        )
        checked = await session.scalar(select(func.clock_timestamp()))
        row.status = "succeeded"
        row.result = {
            "github_repository_id": 101,
            "full_name": "team/private",
            "private": True,
            "default_branch": "main",
            "checked_at": checked.isoformat(),
        }
    view = await env.jobs.get_visible(env.actor, job.id)
    assert view.result.github_repository_id == 101
    assert view.result.checked_at == checked


async def test_read_fences_epoch_change_after_policy_returns(job_env, monkeypatch):
    from devflow.repositories.models import Repository

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "key")
    require = env.policy.require

    async def changed_after_verification(*args, **kwargs):
        view = await require(*args, **kwargs)
        async with env.factory.begin() as session:
            await session.execute(
                update(Repository).values(
                    available=False, auth_version=Repository.auth_version + 1
                )
            )
        return view

    monkeypatch.setattr(env.policy, "require", changed_after_verification)
    with pytest.raises(AppError) as error:
        await env.jobs.get_visible(env.actor, job.id)
    assert error.value.status == 404
