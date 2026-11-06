import asyncio

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from support.repository_env import installation, repository
from support.repository_env import repo_env as repo_env  # noqa: PLC0414

from devflow.errors import AppError


@pytest.fixture
def tmp_path(workspace_tmp_path):
    # Explicit R15 opt-in for new owned fixture files on Windows.
    return workspace_tmp_path


@pytest.mark.asyncio
async def test_admin_cannot_bypass_github_access(repo_env):
    env = repo_env
    env.repositories = {}
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.status == 404
    assert all("access_tokens" not in call.url.path for call in env.calls)


@pytest.mark.asyncio
async def test_numeric_id_binding(repo_env):
    from devflow.repositories.models import Repository

    env = repo_env
    first = await env.service.register(env.actor, 11, 101)
    env.repositories[(11, 1)] = ([repository(102)], None)
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, first.id, fresh=True)
    assert error.value.status == 404
    env.installations = {1: ([installation(12)], None)}
    env.repositories[(12, 1)] = ([repository(101, "moved/renamed")], None)
    moved = await env.service.register(env.actor, 12, 101)
    assert moved.id == first.id and moved.installation_id == 12
    assert moved.full_name == "moved/renamed"
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Repository)) == 1


@pytest.mark.asyncio
async def test_cache_expiry_and_local_version_invalidation(repo_env, monkeypatch):
    from devflow.repositories import policy
    from devflow.repositories.models import Repository

    env = repo_env
    now = [100.0]
    monkeypatch.setattr(policy.time, "monotonic", lambda: now[0])
    registered = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, registered.id)
    env.status = 503
    assert (await env.policy.require(env.actor, registered.id)).id == registered.id
    now[0] = 160.0
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id)
    assert error.value.status == 503
    env.status = 200
    await env.policy.require(env.actor, registered.id)
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository)
            .where(Repository.id == registered.id)
            .values(
                available=False,
                needs_verification=False,
                auth_version=Repository.auth_version + 1,
            )
        )
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id)
    assert error.value.status == 404


@pytest.mark.asyncio
async def test_user_list_filters_private_repositories(repo_env):
    env = repo_env
    env.repositories[(11, 1)] = (
        [repository(101), repository(102), repository(103)],
        None,
    )
    for number in [101, 102, 103]:
        await env.service.register(env.actor, 11, number)
    env.repositories[(11, 1)] = ([repository(102)], None)
    page = await env.service.list_enabled(env.actor, 1, None)
    assert [item.github_repository_id for item in page.items] == [102]
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_concurrent_registration_is_one_record_and_audit(repo_env):
    from devflow.audit.models import AuditLog
    from devflow.repositories.models import Repository

    env = repo_env
    barrier = asyncio.Barrier(2)

    async def register():
        await barrier.wait()
        return await env.service.register(env.actor, 11, 101)

    a, b = await asyncio.gather(register(), register())
    assert a.id == b.id
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Repository)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "repository.registered")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_late_online_result_cannot_restore_revoked_installation(repo_env):
    from devflow.github.models import GitHubInstallation

    env = repo_env
    await env.service.register(env.actor, 11, 101)

    async def revoke(request):
        if request.url.path.endswith("/repositories"):
            async with env.factory.begin() as session:
                await session.execute(
                    update(GitHubInstallation)
                    .where(GitHubInstallation.installation_id == 11)
                    .values(
                        available=False,
                        needs_verification=False,
                        auth_version=GitHubInstallation.auth_version + 1,
                    )
                )

    env.before_response = revoke
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.status == 404
    async with env.factory() as session:
        row = await session.scalar(select(GitHubInstallation))
        assert not row.available


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["installation", "repository", "user"])
async def test_each_local_epoch_rejects_a_late_registration(repo_env, target):
    from devflow.auth.models import User
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)

    async def invalidate(request):
        if not request.url.path.endswith("/repositories"):
            return
        async with env.factory.begin() as session:
            if target == "user":
                await session.execute(
                    update(User)
                    .where(User.id == env.actor.id)
                    .values(auth_version=User.auth_version + 1)
                )
            else:
                model = GitHubInstallation if target == "installation" else Repository
                await session.execute(
                    update(model).values(
                        auth_version=model.auth_version + 1,
                        available=False,
                        needs_verification=True,
                    )
                )

    env.before_response = invalidate
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.status == (401 if target == "user" else 404)
    async with env.factory() as session:
        row = await session.get(Repository, registered.id)
        assert row.auth_version == (2 if target == "repository" else 1)


@pytest.mark.asyncio
async def test_restoration_requires_online_verification_and_preserves_disabled(
    repo_env,
):
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    async with env.factory.begin() as session:
        for model in [GitHubInstallation, Repository]:
            await session.execute(
                update(model).values(
                    available=False,
                    needs_verification=True,
                    auth_version=model.auth_version + 1,
                )
            )
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id)
    assert error.value.status == 404
    env.status = 503
    with pytest.raises(AppError):
        await env.service.register(env.actor, 11, 101)
    async with env.factory() as session:
        assert not (await session.get(Repository, registered.id)).available
    env.status = 200
    restored = await env.service.register(env.actor, 11, 101)
    assert restored.id == registered.id
    assert (await env.policy.require(env.actor, registered.id)).id == registered.id
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository).values(
                enabled=False, auth_version=Repository.auth_version + 1
            )
        )
    with pytest.raises(AppError):
        await env.service.register(env.actor, 11, 101)
    async with env.factory() as session:
        assert not (await session.get(Repository, registered.id)).enabled


@pytest.mark.asyncio
async def test_stale_rest_401_does_not_clear_rotated_tokens(repo_env):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.github.models import GitHubCredential

    env = repo_env

    async def rotate(request):
        if request.url.path == "/user/installations":
            assert request.headers["authorization"] == "Bearer user-token"
            async with env.factory.begin() as session:
                await session.execute(
                    update(GitHubCredential).values(
                        access_token_encrypted=env.cipher.encrypt(
                            SecretStr("rotated-token")
                        ),
                        version=2,
                    )
                )

    env.before_response, env.status = rotate, 401
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.code == "reauth_required"
    async with env.factory() as session:
        credential = await session.scalar(select(GitHubCredential))
        assert (
            env.cipher.decrypt(credential.access_token_encrypted).get_secret_value()
            == "rotated-token"
        )
        assert not credential.needs_reauth
        assert (await session.get(User, env.actor.id)).auth_version == 1


@pytest.mark.asyncio
async def test_current_rest_401_invalidates_user_credentials(repo_env):
    from devflow.auth.models import User
    from devflow.github.models import GitHubCredential

    env = repo_env
    env.status = 401
    with pytest.raises(AppError) as error:
        await env.service.discover(env.actor, 20, None)
    assert error.value.code == "reauth_required"
    async with env.factory() as session:
        credential = await session.scalar(select(GitHubCredential))
        assert credential.needs_reauth and credential.access_token_encrypted is None
        assert (await session.get(User, env.actor.id)).auth_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 429, 503])
async def test_failed_fresh_check_discards_cached_success(repo_env, status):
    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, registered.id)
    env.status = status
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id, fresh=True)
    assert error.value.status == (404 if status in (403, 404) else 503)
    with pytest.raises(AppError):
        await env.policy.require(env.actor, registered.id)


@pytest.mark.asyncio
async def test_registration_audit_failure_rolls_back_state(repo_env):
    from sqlalchemy import text

    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env
    async with env.factory.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_repository_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'repository.registered' THEN RAISE EXCEPTION 'audit unavailable'; END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_repository_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_repository_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await env.service.register(env.actor, 11, 101)
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Repository)) == 0
        assert not (await session.scalar(select(GitHubInstallation))).available


@pytest.mark.asyncio
async def test_repository_denial_is_audited(repo_env):
    from devflow.audit.models import AuditLog

    env = repo_env
    env.repositories = {}
    with pytest.raises(AppError):
        await env.service.register(env.actor, 11, 101)
    async with env.factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "permission.denied")
        )
        assert audit is not None
        assert audit.outcome == "failure" and audit.reason_code == "resource_not_found"


@pytest.mark.asyncio
async def test_new_registration_cannot_cross_installation_epoch(repo_env):
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env

    async def removed_unknown_repo(request):
        if request.url.path.endswith("/repositories"):
            async with env.factory.begin() as session:
                await session.execute(
                    update(GitHubInstallation).values(
                        auth_version=GitHubInstallation.auth_version + 1
                    )
                )

    env.before_response = removed_unknown_repo
    with pytest.raises(AppError) as error:
        await env.service.register(env.actor, 11, 101)
    assert error.value.status == 404
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Repository)) == 0


@pytest.mark.asyncio
async def test_list_rechecks_previously_collected_items_after_later_provider_calls(
    repo_env,
):
    from datetime import UTC, datetime

    from devflow.repositories.models import Repository

    env = repo_env
    env.repositories[(11, 1)] = ([repository(101), repository(102)], None)
    await env.service.register(env.actor, 11, 101)
    newer = await env.service.register(env.actor, 11, 102)
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        await session.execute(
            update(Repository)
            .where(Repository.id == newer.id)
            .values(created_at=datetime(2026, 1, 2, tzinfo=UTC))
        )
    calls = 0

    async def revoke_previous(request):
        nonlocal calls
        if request.url.path.endswith("/repositories"):
            calls += 1
            if calls == 2:
                async with env.factory.begin() as session:
                    await session.execute(
                        update(Repository)
                        .where(Repository.id == newer.id)
                        .values(
                            available=False,
                            needs_verification=False,
                            auth_version=Repository.auth_version + 1,
                        )
                    )

    env.before_response = revoke_previous
    page = await env.service.list_enabled(env.actor, 20, None)
    assert [item.github_repository_id for item in page.items] == [101]


@pytest.mark.asyncio
async def test_reverification_changes_are_audited_atomically(repo_env):
    from devflow.audit.models import AuditLog
    from devflow.repositories.models import Repository

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository).values(
                available=False,
                needs_verification=True,
                auth_version=Repository.auth_version + 1,
            )
        )
    await env.service.register(env.actor, 11, 101)
    async with env.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "repository.registered",
                    AuditLog.resource_id == registered.id,
                )
            )
            == 2
        )


@pytest.mark.asyncio
async def test_positive_cache_never_crosses_users(repo_env):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.models import GitHubCredential

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, registered.id)
    async with env.factory.begin() as session:
        user = User(github_id=43, role="member")
        session.add(user)
        await session.flush()
        session.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=env.cipher.encrypt(
                    SecretStr("second-user-token")
                ),
            )
        )
        other = Actor(user.id, user.github_id, user.role, user.auth_version)
    env.repositories = {}
    assert (await env.service.list_enabled(other, 20, None)).items == []
    assert (await env.policy.require(env.actor, registered.id)).id == registered.id


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["installation", "repository", "user"])
async def test_cache_checks_each_local_version_on_every_read(repo_env, target):
    from devflow.auth.models import User
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    await env.policy.require(env.actor, registered.id)
    model = {
        "installation": GitHubInstallation,
        "repository": Repository,
        "user": User,
    }[target]
    async with env.factory.begin() as session:
        await session.execute(update(model).values(auth_version=model.auth_version + 1))
    env.status = 503
    with pytest.raises(AppError) as error:
        await env.policy.require(env.actor, registered.id)
    assert error.value.status == (401 if target == "user" else 503)


@pytest.mark.asyncio
async def test_local_repository_keyset_cursor_handles_ties_and_purpose(repo_env):
    from datetime import UTC, datetime

    from devflow.repositories.models import Repository

    env = repo_env
    env.repositories[(11, 1)] = (
        [repository(101), repository(102), repository(103)],
        None,
    )
    registered = [
        await env.service.register(env.actor, 11, number) for number in [101, 102, 103]
    ]
    async with env.factory.begin() as session:
        await session.execute(
            update(Repository).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    ids, cursor = [], None
    first_cursor = None
    while True:
        page = await env.service.list_enabled(env.actor, 1, cursor)
        ids.extend(item.id for item in page.items)
        cursor = page.next_cursor
        first_cursor = first_cursor or cursor
        if cursor is None:
            break
    assert ids == sorted([item.id for item in registered], reverse=True)
    with pytest.raises(AppError) as error:
        await env.service.discover(env.actor, 1, first_cursor)
    assert error.value.status == 400


@pytest.mark.asyncio
async def test_late_denial_clears_concurrently_populated_success_cache(repo_env):
    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    started, release = asyncio.Event(), asyncio.Event()
    first = True

    async def delay_first(request):
        nonlocal first
        if request.url.path.endswith("/repositories") and first:
            first = False
            started.set()
            await release.wait()

    env.before_response = delay_first
    pending = asyncio.create_task(
        env.policy.require(env.actor, registered.id, fresh=True)
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        await env.policy.require(env.actor, registered.id, fresh=True)
        env.status = 403
        release.set()
        with pytest.raises(AppError):
            await pending
        with pytest.raises(AppError) as error:
            await env.policy.require(env.actor, registered.id)
        assert error.value.status == 404
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def reader_actor(env):
    from pydantic import SecretStr

    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.models import GitHubCredential

    async with env.factory.begin() as session:
        user = User(github_id=77, role="member")
        session.add(user)
        await session.flush()
        session.add(
            GitHubCredential(
                user_id=user.id,
                access_token_encrypted=env.cipher.encrypt(SecretStr("reader-token")),
            )
        )
        return Actor(user.id, user.github_id, user.role, user.auth_version)


@pytest.mark.asyncio
async def test_fresh_require_returns_metadata_from_verified_provider_response(repo_env):
    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    env.repositories[(11, 1)] = ([repository(101, "team/renamed")], None)
    view = await env.policy.require(env.actor, registered.id, fresh=True)
    assert view.id == registered.id and view.full_name == "team/renamed"


@pytest.mark.asyncio
async def test_require_rejects_transfer_after_verified_transaction(
    repo_env, monkeypatch
):
    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    reader = await reader_actor(env)
    reached, release = asyncio.Event(), asyncio.Event()
    real_local = env.policy.local
    reads = 0

    async def pause_before_final_read(actor, repository_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            # The real verification transaction committed before this query.
            reached.set()
            await release.wait()
        return await real_local(actor, repository_id)

    monkeypatch.setattr(env.policy, "local", pause_before_final_read)
    pending = asyncio.create_task(env.policy.require(reader, registered.id, fresh=True))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        env.installations = {1: ([installation(12)], None)}
        env.repositories[(12, 1)] = ([repository(101, "other/private-transfer")], None)
        moved = await env.service.register(env.actor, 12, 101)
        assert moved.id == registered.id and moved.installation_id == 12
        # The member's provider view excludes the destination installation.
        env.installations = {1: ([installation(11)], None)}
        release.set()
        with pytest.raises(AppError) as error:
            await pending
        assert error.value.status == 404
        with pytest.raises(AppError) as error:
            await env.policy.require(reader, registered.id)
        assert error.value.status == 404
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["installation", "repository"])
async def test_require_cannot_restore_state_changed_after_first_local_read(
    repo_env, monkeypatch, target
):
    from devflow.audit.models import AuditLog
    from devflow.github.models import GitHubInstallation
    from devflow.repositories.models import Repository

    env = repo_env
    registered = await env.service.register(env.actor, 11, 101)
    reader = await reader_actor(env)
    reached, release = asyncio.Event(), asyncio.Event()
    real_local = env.policy.local
    first = True

    async def pause_after_initial_read(actor, repository_id):
        nonlocal first
        result = await real_local(actor, repository_id)
        if first:
            first = False
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(env.policy, "local", pause_after_initial_read)
    pending = asyncio.create_task(env.policy.require(reader, registered.id, fresh=True))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with env.factory.begin() as session:
            # Every scope event increments the installation epoch; revoke then
            # restoration marker leave remote availability unpromoted.
            await session.execute(
                update(GitHubInstallation).values(
                    auth_version=GitHubInstallation.auth_version + 2,
                    **(
                        {"available": False, "needs_verification": True}
                        if target == "installation"
                        else {}
                    ),
                )
            )
            if target == "repository":
                await session.execute(
                    update(Repository)
                    .where(Repository.id == registered.id)
                    .values(
                        available=False,
                        needs_verification=True,
                        auth_version=Repository.auth_version + 2,
                    )
                )
        release.set()
        with pytest.raises(AppError) as error:
            await pending
        assert error.value.status == 404
        async with env.factory() as session:
            model = GitHubInstallation if target == "installation" else Repository
            row = await session.scalar(select(model))
            assert (
                not row.available and row.needs_verification and row.auth_version == 3
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "repository.registered")
                )
                == 1
            )
        with pytest.raises(AppError) as error:
            await env.policy.require(reader, registered.id)
        assert error.value.status == 404
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
