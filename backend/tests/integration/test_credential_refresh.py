import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from devflow.audit.models import AuditLog
from devflow.auth.models import AUTH_ORDER_SEQUENCE, User
from devflow.auth.types import Actor
from devflow.db import create_session_factory
from devflow.errors import AppError
from devflow.github.client import GitHubClient
from devflow.github.models import GitHubCredential
from devflow.security import CredentialCipher

pytestmark = pytest.mark.asyncio
OLD_ACCESS = "synthetic-old-access-sentinel"
OLD_REFRESH = "synthetic-old-refresh-sentinel"
NEW_ACCESS = "synthetic-new-access-sentinel"
NEW_REFRESH = "synthetic-new-refresh-sentinel"


@pytest.fixture
def cipher():
    return CredentialCipher(Fernet.generate_key())


@pytest.fixture
def github_settings(settings_factory, workspace_tmp_path):
    secret = workspace_tmp_path / "client-secret"
    secret.write_text("synthetic-client-secret")
    return settings_factory(
        github_client_id="client-id", github_client_secret_file=secret
    )


async def seed(factory, cipher, **overrides):
    async with factory.begin() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        user = User(github_id=123, role="member")
        session.add(user)
        await session.flush()
        values = {
            "user_id": user.id,
            "access_token_encrypted": cipher.encrypt(SecretStr(OLD_ACCESS)),
            "refresh_token_encrypted": cipher.encrypt(SecretStr(OLD_REFRESH)),
            "expires_at": now - timedelta(minutes=1),
            "refresh_expires_at": now + timedelta(days=30),
        }
        values.update(overrides)
        session.add(GitHubCredential(**values))
        return Actor(user.id, user.github_id, "member", user.auth_version)


def refreshed_response():
    return httpx.Response(
        200,
        json={
            "access_token": NEW_ACCESS,
            "refresh_token": NEW_REFRESH,
            "expires_in": 28800,
            "refresh_token_expires_in": 15811200,
            "token_type": "bearer",
        },
    )


async def snapshot(factory):
    async with factory() as session:
        credential = await session.scalar(select(GitHubCredential))
        user = await session.scalar(select(User))
        audits = list(await session.scalars(select(AuditLog)))
        return credential, user, audits


async def wait_for_lock(factory, blocker, task):
    async with asyncio.timeout(5), factory() as observer:
        while not await observer.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted "
                "AND :blocker = ANY(pg_blocking_pids(pid)))"
            ),
            {"blocker": blocker},
        ):
            if task.done():
                await task
                pytest.fail("Operation bypassed the user lock")
            await asyncio.sleep(0)


async def wait_for_credential_waiter(factory):
    async with asyncio.timeout(5), factory() as observer:
        while not await observer.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks waiting JOIN pg_locks holding "
                "ON holding.pid = ANY(pg_blocking_pids(waiting.pid)) "
                "WHERE NOT waiting.granted AND holding.relation = 'users'::regclass)"
            )
        ):
            await asyncio.sleep(0)


async def test_cross_process_refresh_once(
    db_factory, migrated_database, cipher, github_settings, caplog
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    other_factory = create_session_factory(migrated_database[0])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(request):
        calls.append(request)
        assert request.url.path == "/login/oauth/access_token"
        assert parse_qs(request.content.decode())["refresh_token"] == [OLD_REFRESH]
        entered.set()
        await release.wait()
        return refreshed_response()

    tasks = []
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            github = GitHubClient(http, github_settings)
            barrier = asyncio.Barrier(2)

            async def run(factory):
                await barrier.wait()
                return await CredentialService(factory, github, cipher).get_user_token(
                    actor
                )

            tasks = [asyncio.create_task(run(f)) for f in (db_factory, other_factory)]
            await asyncio.wait_for(entered.wait(), 5)
            # While the network call holds row locks, no partial pair is visible.
            before, _, _ = await snapshot(db_factory)
            assert (
                cipher.decrypt(before.access_token_encrypted).get_secret_value()
                == OLD_ACCESS
            )
            assert (
                cipher.decrypt(before.refresh_token_encrypted).get_secret_value()
                == OLD_REFRESH
            )
            await wait_for_credential_waiter(db_factory)
            release.set()
            tokens = await asyncio.gather(*tasks)
        assert [t.get_secret_value() for t in tokens] == [NEW_ACCESS, NEW_ACCESS]
        assert len(calls) == 1
        row, user, _ = await snapshot(db_factory)
        assert row.version == 2 and not row.needs_reauth and user.auth_version == 1
        assert (
            cipher.decrypt(row.access_token_encrypted).get_secret_value() == NEW_ACCESS
        )
        assert (
            cipher.decrypt(row.refresh_token_encrypted).get_secret_value()
            == NEW_REFRESH
        )
        assert row.expires_at > datetime.now(UTC)
        assert row.refresh_expires_at > row.expires_at
        for token in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH):
            assert (
                token.encode()
                not in row.access_token_encrypted + row.refresh_token_encrypted
            )
            assert token not in caplog.text
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await other_factory.kw["bind"].dispose()


@pytest.mark.parametrize("expiry", [None, "future"])
async def test_valid_access_never_refreshes(
    db_factory, cipher, github_settings, expiry
):
    from devflow.github.credentials import CredentialService

    expires_at = None if expiry is None else datetime.now(UTC) + timedelta(hours=1)
    actor = await seed(db_factory, cipher, expires_at=expires_at)

    def handle(request):
        pytest.fail("Valid token caused a remote call")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        token = await CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        ).get_user_token(actor)
    assert token.get_secret_value() == OLD_ACCESS
    assert (await snapshot(db_factory))[0].version == 1


@pytest.mark.parametrize(
    "failure,expected,terminal",
    [
        ("bad_refresh_token", "reauth_required", True),
        ("incorrect_client_credentials", "github_configuration_error", False),
        ("timeout", "github_unavailable", False),
        ("5xx", "github_unavailable", False),
        ("rate_limit", "github_rate_limited", False),
        ("unknown", "github_unavailable", False),
    ],
)
async def test_refresh_failure_classification(
    db_factory, cipher, github_settings, failure, expected, terminal, caplog
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    original, original_user, _ = await snapshot(db_factory)
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(OLD_REFRESH)
        if failure == "5xx":
            return httpx.Response(503, json={"error": "bad_refresh_token"})
        if failure == "rate_limit":
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(
            200, json={"error": failure, "error_description": OLD_REFRESH}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        service = CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        )
        with pytest.raises(AppError) as caught:
            await service.get_user_token(actor)
    assert caught.value.code == expected
    assert len(calls) == 1 and calls[0].url.host == "github.com"
    row, user, audits = await snapshot(db_factory)
    if terminal:
        assert row.needs_reauth and row.version == 2
        assert row.access_token_encrypted is row.refresh_token_encrypted is None
        assert row.expires_at is row.refresh_expires_at is None
        assert (
            user.auth_version == 2
            and user.auth_changed_order > original_user.auth_changed_order
        )
        assert user.enabled
        assert len(audits) == 1
        assert (audits[0].action, audits[0].resource_id, audits[0].reason_code) == (
            "credential.revoked",
            row.id,
            "reauth_required",
        )
    else:
        assert row.version == 1 and not row.needs_reauth
        assert row.access_token_encrypted == original.access_token_encrypted
        assert row.refresh_token_encrypted == original.refresh_token_encrypted
        assert user.auth_version == 1 and not audits
    assert OLD_REFRESH not in str(caught.value) + caplog.text


@pytest.mark.parametrize("refresh", ["missing", "expired"])
async def test_unrefreshable_expired_access_requires_reauth(
    db_factory, cipher, github_settings, refresh
):
    from devflow.github.credentials import CredentialService

    async with db_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
    changes = (
        {"refresh_token_encrypted": None}
        if refresh == "missing"
        else {"refresh_expires_at": now - timedelta(seconds=1)}
    )
    actor = await seed(db_factory, cipher, **changes)

    def handle(request):
        pytest.fail("Unusable refresh token sent upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AppError, match="authorization"):
            await CredentialService(
                db_factory, GitHubClient(http, github_settings), cipher
            ).get_user_token(actor)
    row, user, audits = await snapshot(db_factory)
    assert row.needs_reauth and row.version == user.auth_version == 2
    assert len(audits) == 1


async def test_waiter_rereads_credentials_after_user_lock(
    db_factory, cipher, github_settings
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)

    def handle(request):
        pytest.fail("Waiter refreshed a stale snapshot")

    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = CredentialService(
                db_factory, GitHubClient(http, github_settings), cipher
            )
            async with db_factory.begin() as writer:
                await writer.scalar(
                    select(User).where(User.id == actor.id).with_for_update()
                )
                blocker = await writer.scalar(text("SELECT pg_backend_pid()"))
                row = await writer.scalar(select(GitHubCredential).with_for_update())
                task = asyncio.create_task(service.get_user_token(actor))
                await wait_for_lock(db_factory, blocker, task)
                row.access_token_encrypted = cipher.encrypt(SecretStr(NEW_ACCESS))
                row.refresh_token_encrypted = cipher.encrypt(SecretStr(NEW_REFRESH))
                row.expires_at = datetime.now(UTC) + timedelta(hours=8)
                row.version += 1
            assert (await task).get_secret_value() == NEW_ACCESS
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_late_401_does_not_clear_new_pair_or_new_authorization(
    db_factory, cipher, github_settings
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: refreshed_response())
    ) as http:
        service = CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        )
        current = await service.get_user_token(actor)
        assert not await service.invalidate_user_token(actor, SecretStr(OLD_ACCESS))
        row, _, audits = await snapshot(db_factory)
        assert row.version == 2 and not row.needs_reauth and not audits
        async with db_factory.begin() as session:
            user = await session.scalar(select(User).with_for_update())
            user.auth_version += 1
            user.auth_changed_at = func.clock_timestamp()
            user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
        assert not await service.invalidate_user_token(actor, current)
        assert (await snapshot(db_factory))[0].version == 2


async def test_current_401_revokes_once(db_factory, cipher, github_settings):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    async with httpx.AsyncClient() as http:
        service = CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        )
        assert await service.invalidate_user_token(actor, SecretStr(OLD_ACCESS))
        assert not await service.invalidate_user_token(actor, SecretStr(OLD_ACCESS))
    row, user, audits = await snapshot(db_factory)
    assert row.needs_reauth and row.version == user.auth_version == 2
    assert row.access_token_encrypted is row.refresh_token_encrypted is None
    assert len(audits) == 1


@pytest.mark.parametrize("change", ["disabled", "new_auth", "reauth"])
async def test_revocation_committed_while_refresh_waits_is_not_restored(
    db_factory, cipher, github_settings, change
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)

    def handle(request):
        pytest.fail("Revoked actor sent a refresh request")

    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = CredentialService(
                db_factory, GitHubClient(http, github_settings), cipher
            )
            async with db_factory.begin() as writer:
                user = await writer.scalar(select(User).with_for_update())
                blocker = await writer.scalar(text("SELECT pg_backend_pid()"))
                row = await writer.scalar(select(GitHubCredential).with_for_update())
                task = asyncio.create_task(service.get_user_token(actor))
                await wait_for_lock(db_factory, blocker, task)
                if change == "disabled":
                    user.enabled = False
                elif change == "new_auth":
                    user.auth_version += 1
                    user.auth_changed_at = func.clock_timestamp()
                    user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
                else:
                    row.needs_reauth = True
                    row.access_token_encrypted = row.refresh_token_encrypted = None
                    row.version += 1
            with pytest.raises(AppError) as caught:
                await task
            assert caught.value.code in {"unauthenticated", "reauth_required"}
        row, user, audits = await snapshot(db_factory)
        assert not audits
        assert row.version == (2 if change == "reauth" else 1)
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_audit_failure_rolls_back_revocation(db_factory, cipher, github_settings):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    async with db_factory.begin() as session:
        await session.execute(
            text(
                "ALTER TABLE audit_logs ADD CONSTRAINT reject_revoke CHECK (action <> 'credential.revoked')"
            )
        )
    async with httpx.AsyncClient() as http:
        service = CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        )
        with pytest.raises(IntegrityError):
            await service.invalidate_user_token(actor, SecretStr(OLD_ACCESS))
    row, user, audits = await snapshot(db_factory)
    assert row.version == user.auth_version == 1 and not row.needs_reauth
    assert cipher.decrypt(row.access_token_encrypted).get_secret_value() == OLD_ACCESS
    assert not audits


async def test_cipher_configuration_error_preserves_pair(
    db_factory, cipher, github_settings
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    async with httpx.AsyncClient() as http:
        service = CredentialService(
            db_factory,
            GitHubClient(http, github_settings),
            CredentialCipher(Fernet.generate_key()),
        )
        with pytest.raises(AppError) as caught:
            await service.get_user_token(actor)
    assert caught.value.code == "github_configuration_error"
    row, user, audits = await snapshot(db_factory)
    assert row.version == user.auth_version == 1 and not row.needs_reauth and not audits


async def test_pair_replacement_rolls_back_together(
    db_factory, cipher, github_settings
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    original, _, _ = await snapshot(db_factory)
    async with db_factory.begin() as session:
        await session.execute(
            text(
                "ALTER TABLE github_credentials ADD CONSTRAINT reject_rotation CHECK (version = 1)"
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: refreshed_response())
    ) as http:
        service = CredentialService(
            db_factory, GitHubClient(http, github_settings), cipher
        )
        with pytest.raises(IntegrityError):
            await service.get_user_token(actor)
    row, user, audits = await snapshot(db_factory)
    assert row.access_token_encrypted == original.access_token_encrypted
    assert row.refresh_token_encrypted == original.refresh_token_encrypted
    assert row.expires_at == original.expires_at
    assert row.refresh_expires_at == original.refresh_expires_at
    assert row.version == user.auth_version == 1 and not audits


@pytest.mark.parametrize("waiting_operation", ["old_401", "disable"])
async def test_late_refresh_cannot_restore_concurrent_revocation(
    db_factory, cipher, github_settings, waiting_operation
):
    from devflow.github.credentials import CredentialService

    actor = await seed(db_factory, cipher)
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def handle(request):
        entered.set()
        await release.wait()
        return refreshed_response()

    async def disable():
        async with db_factory.begin() as session:
            user = await session.scalar(
                select(User).where(User.id == actor.id).with_for_update()
            )
            row = await session.scalar(
                select(GitHubCredential)
                .where(GitHubCredential.user_id == actor.id)
                .with_for_update()
            )
            user.enabled = False
            user.auth_version += 1
            user.auth_changed_at = func.clock_timestamp()
            user.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
            row.access_token_encrypted = row.refresh_token_encrypted = None
            row.needs_reauth = True
            row.version += 1

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            service = CredentialService(
                db_factory, GitHubClient(http, github_settings), cipher
            )
            tasks.append(asyncio.create_task(service.get_user_token(actor)))
            await asyncio.wait_for(entered.wait(), 5)
            waiter = (
                service.invalidate_user_token(actor, SecretStr(OLD_ACCESS))
                if waiting_operation == "old_401"
                else disable()
            )
            tasks.append(asyncio.create_task(waiter))
            await wait_for_credential_waiter(db_factory)
            release.set()
            token, result = await asyncio.gather(*tasks)
            assert token.get_secret_value() == NEW_ACCESS
            row, user, audits = await snapshot(db_factory)
            if waiting_operation == "old_401":
                assert result is False
                assert row.version == 2 and not row.needs_reauth and not audits
                assert (
                    cipher.decrypt(row.access_token_encrypted).get_secret_value()
                    == NEW_ACCESS
                )
            else:
                assert not user.enabled and user.auth_version == 2
                assert row.version == 3 and row.needs_reauth
                assert row.access_token_encrypted is row.refresh_token_encrypted is None
                with pytest.raises(AppError) as caught:
                    await service.get_user_token(actor)
                assert caught.value.code == "unauthenticated"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
