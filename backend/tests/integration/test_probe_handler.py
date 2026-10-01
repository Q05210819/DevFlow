import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from support.job_env import job_env as job_env  # noqa: PLC0414
from support.repository_env import repo_env as repo_env  # noqa: PLC0414
from support.repository_env import repository


async def execute(env, job_id):
    from devflow.jobs.handlers import run_probe

    return await run_probe(job_id, env.factory, env.policy, env.github, env.credentials)


@pytest.mark.asyncio
async def test_probe_rechecks_creator_authorization(job_env):
    from devflow.auth.models import AUTH_ORDER_SEQUENCE, User
    from devflow.errors import AppError

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "disabled-creator")
    async with env.factory.begin() as session:
        user = await session.get(User, env.actor.id, with_for_update=True)
        user.enabled = False
        user.auth_version += 1
        user.auth_changed_at = func.clock_timestamp()
        user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
    env.calls.clear()
    with pytest.raises(AppError) as error:
        await execute(env, job.id)
    assert error.value.code in {"permission_revoked", "unauthenticated"}
    assert env.calls == []


@pytest.mark.asyncio
async def test_probe_bypasses_positive_cache_and_uses_numeric_id(job_env):
    from devflow.errors import AppError

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "fresh-probe")
    env.repositories[(11, 1)] = ([repository(999, "team/private")], None)
    with pytest.raises(AppError) as error:
        await execute(env, job.id)
    assert error.value.code == "resource_not_found"


@pytest.mark.asyncio
async def test_probe_returns_fresh_read_only_metadata_with_database_timestamp(job_env):
    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "metadata")
    env.repositories[(11, 1)] = ([repository(101, "team/renamed")], None)
    env.calls.clear()
    value = await execute(env, job.id)
    assert value.github_repository_id == 101
    assert value.full_name == "team/renamed"
    assert value.private is True
    assert value.default_branch == "main"
    assert value.checked_at.tzinfo is not None
    assert env.calls and all(r.method == "GET" for r in env.calls)
    assert all(r.url.host == "api.github.com" for r in env.calls)
    assert all(
        r.headers["authorization"] == "Bearer user-token"
        for r in env.calls
        if r.url.path.startswith("/user/")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["user", "installation", "repository", "epoch"])
async def test_probe_rechecks_local_epochs_after_metadata_response(job_env, target):
    from devflow.auth.models import AUTH_ORDER_SEQUENCE, User
    from devflow.errors import AppError
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "late-revoke")
    repository_calls = 0

    async def revoke(request):
        nonlocal repository_calls
        if not request.url.path.endswith("/repositories"):
            return
        repository_calls += 1
        if repository_calls != 2:
            return
        async with env.factory.begin() as session:
            if target == "user":
                row = await session.get(User, env.actor.id, with_for_update=True)
                row.auth_changed_at = func.clock_timestamp()
                row.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
                row.enabled = False
            elif target == "installation":
                row = await session.scalar(
                    select(GitHubInstallation)
                    .where(GitHubInstallation.installation_id == 11)
                    .with_for_update()
                )
                row.available = False
            else:
                row = await session.get(
                    Repository, env.repository.id, with_for_update=True
                )
                if target != "epoch":
                    row.available = False
            row.auth_version += 1

    env.before_response = revoke
    with pytest.raises(AppError) as error:
        await execute(env, job.id)
    assert repository_calls == 2
    assert error.value.code in {
        "resource_not_found",
        "permission_revoked",
        "unauthenticated",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("after_check", [False, True])
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "reauth_required"),
        (403, "resource_not_found"),
        (500, "github_unavailable"),
    ],
)
async def test_probe_provider_errors_only_revoke_confirmed_current_token(
    job_env, status, expected, after_check
):
    from devflow.errors import AppError
    from devflow.github.models import GitHubCredential

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "provider-error")
    repository_calls = 0

    async def fail_metadata(request):
        nonlocal repository_calls
        if request.url.path.endswith("/repositories"):
            repository_calls += 1
            if repository_calls == 2:
                env.status = status

    if after_check:
        env.before_response = fail_metadata
    else:
        env.status = status
    with pytest.raises(AppError) as error:
        await execute(env, job.id)
    assert error.value.code == expected
    async with env.factory() as session:
        credential = await session.scalar(
            select(GitHubCredential).where(GitHubCredential.user_id == env.actor.id)
        )
        assert credential.needs_reauth is (status == 401)
        assert (credential.access_token_encrypted is None) is (status == 401)


@pytest.mark.asyncio
async def test_probe_late_401_does_not_clear_rotated_token(job_env):
    from devflow.errors import AppError
    from devflow.github.models import GitHubCredential

    env = job_env
    job = await env.jobs.submit_probe(env.actor, env.repository.id, "old-token")
    repository_calls = 0

    async def rotate(request):
        nonlocal repository_calls
        if not request.url.path.endswith("/repositories"):
            return
        repository_calls += 1
        if repository_calls != 2:
            return
        async with env.factory.begin() as session:
            row = await session.scalar(
                select(GitHubCredential)
                .where(GitHubCredential.user_id == env.actor.id)
                .with_for_update()
            )
            row.access_token_encrypted = env.cipher.encrypt(SecretStr("rotated-token"))
            row.version += 1
        env.status = 401

    env.before_response = rotate
    with pytest.raises(AppError) as error:
        await execute(env, job.id)
    assert error.value.code == "reauth_required"
    async with env.factory() as session:
        row = await session.scalar(
            select(GitHubCredential).where(GitHubCredential.user_id == env.actor.id)
        )
        assert row.needs_reauth is False
        assert (
            env.cipher.decrypt(row.access_token_encrypted).get_secret_value()
            == "rotated-token"
        )
