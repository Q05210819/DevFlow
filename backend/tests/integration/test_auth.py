import asyncio
import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, update
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member

from devflow.audit.models import AuditLog
from devflow.auth.models import AUTH_ORDER_SEQUENCE, OAuthState, Session, User
from devflow.errors import AppError
from devflow.github.models import GitHubCredential

pytestmark = pytest.mark.asyncio


async def start(env):
    result = await env.service.begin_login("/repositories")
    return result, parse_qs(urlsplit(result.authorize_url).query)["state"][0]


async def test_oauth_state_is_single_use_and_browser_bound(auth_env):
    env = auth_env
    await member(env)
    begun, state = await start(env)
    with pytest.raises(AppError) as rejected:
        await env.service.complete_login("code", state, "wrong-browser")
    assert rejected.value.status == 400
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
    issue = await env.service.complete_login(
        "code", state, begun.browser_nonce.get_secret_value()
    )
    assert issue.return_path == "/repositories"
    with pytest.raises(AppError):
        await env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    async with env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        row = await db.scalar(select(OAuthState))
        assert row.state_hash == hashlib.sha256(state.encode()).hexdigest()
        verifier = env.cipher.decrypt(row.pkce_verifier_encrypted).get_secret_value()
        challenge = parse_qs(urlsplit(begun.authorize_url).query)["code_challenge"][0]
        assert (
            challenge
            == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert row.expires_at - row.created_at == timedelta(minutes=10)
        assert row.consumed_at is not None


async def test_state_expires_at_ten_minute_boundary(auth_env):
    await member(auth_env)
    begun, state = await start(auth_env)
    async with auth_env.factory.begin() as db:
        await db.execute(update(OAuthState).values(expires_at=func.clock_timestamp()))
    with pytest.raises(AppError) as rejected:
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    assert rejected.value.status == 400
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert (await db.scalar(select(AuditLog.reason_code))) == "state_expired"


async def test_login_requires_allowlist(auth_env):
    await member(auth_env, github_id=102)
    begun, state = await start(auth_env)
    with pytest.raises(AppError) as rejected:
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    assert rejected.value.status == 403
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(GitHubCredential)) == 0
        audit = await db.scalar(select(AuditLog))
        assert (audit.outcome, audit.reason_code) == ("failure", "member_not_allowed")


async def test_concurrent_callback_only_issues_one_session(auth_env):
    await member(auth_env)
    begun, state = await start(auth_env)
    barrier = asyncio.Barrier(2)

    async def callback():
        await barrier.wait()
        return await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )

    results = await asyncio.gather(callback(), callback(), return_exceptions=True)
    assert sum(isinstance(value, AppError) for value in results) == 1
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        credential = await db.scalar(select(GitHubCredential))
        assert (
            auth_env.cipher.decrypt(
                credential.access_token_encrypted
            ).get_secret_value()
            == "synthetic-access"
        )
        assert (
            auth_env.cipher.decrypt(
                credential.refresh_token_encrypted
            ).get_secret_value()
            == "synthetic-refresh"
        )
        assert (await db.scalar(select(User))).login == "renamed-user"


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test",
        "//evil.test",
        "/\\evil.test",
        "/%2f%2fevil.test",
        "/repositories/../evil",
        "/unknown",
        "/\r\nLocation:x",
    ],
)
async def test_return_path_is_allowlisted(auth_env, path):
    with pytest.raises(AppError) as error:
        await auth_env.service.begin_login(path)
    assert error.value.status == 400


async def test_oauth_routes_redirect_and_do_not_expose_tokens(auth_env):
    await member(auth_env)
    async with browser(auth_env) as client:
        begin = await client.get(
            "/api/v1/auth/github/start", params={"return_path": "/repositories"}
        )
        assert begin.status_code == 302
        state = parse_qs(urlsplit(begin.headers["location"]).query)["state"][0]
        response = await client.get(
            "/api/v1/auth/github/callback", params={"code": "code", "state": state}
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/repositories"
        assert "synthetic-access" not in response.text
        assert "synthetic-refresh" not in str(response.headers)
        assert (await client.get("/api/v1/session")).status_code == 200


async def test_provider_failure_consumes_state_without_creating_session(auth_env):
    await member(auth_env)
    begun, state = await start(auth_env)
    auth_env.token_status = 503
    with pytest.raises(AppError) as error:
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    assert error.value.status == 503
    auth_env.token_status = 200
    with pytest.raises(AppError):
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        reasons = list(await db.scalars(select(AuditLog.reason_code)))
        assert "github_unavailable" in reasons


async def test_login_rolls_back_credentials_session_and_profile_if_audit_fails(
    auth_env,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    target = await member(auth_env)
    begun, state = await start(auth_env)
    async with auth_env.factory.begin() as db:
        await db.execute(
            text(
                "CREATE FUNCTION reject_login_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'auth.login' AND NEW.outcome = 'success' THEN RAISE EXCEPTION 'test audit failure'; END IF; RETURN NEW; END $$"
            )
        )
        await db.execute(
            text(
                "CREATE TRIGGER reject_login_audit BEFORE INSERT ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_login_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(GitHubCredential)) == 0
        assert (await db.get(User, target.id)).login is None
        assert (await db.scalar(select(OAuthState))).consumed_at is not None


async def test_login_respects_revoke_committed_while_waiting_for_user_lock(auth_env):
    from sqlalchemy import text

    target = await member(auth_env)
    begun, state = await start(auth_env)
    callback = None
    try:
        async with auth_env.factory.begin() as revoker:
            row = await revoker.scalar(
                select(User).where(User.id == target.id).with_for_update()
            )
            blocker = await revoker.scalar(text("SELECT pg_backend_pid()"))
            callback = asyncio.create_task(
                auth_env.service.complete_login(
                    "code", state, begun.browser_nonce.get_secret_value()
                )
            )
            async with asyncio.timeout(5), auth_env.factory() as observer:
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": blocker},
                ):
                    if callback.done():
                        await callback
                        pytest.fail("Login bypassed user row lock")
                    await asyncio.sleep(0)
            row.auth_version += 1
            row.auth_changed_at = await revoker.scalar(select(func.clock_timestamp()))
            row.auth_changed_order = AUTH_ORDER_SEQUENCE.next_value()
        with pytest.raises(AppError) as error:
            await callback
        assert error.value.status == 403
        async with auth_env.factory() as db:
            assert await db.scalar(select(func.count()).select_from(Session)) == 0
            assert (
                await db.scalar(select(func.count()).select_from(GitHubCredential)) == 0
            )
    finally:
        if callback and not callback.done():
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)


async def test_clock_rollback_does_not_reject_state_started_after_member_exists(
    auth_env,
):
    target = await member(auth_env)
    async with auth_env.factory.begin() as db:
        await db.execute(
            update(User)
            .where(User.id == target.id)
            .values(auth_changed_at=func.clock_timestamp() + timedelta(days=1))
        )
    begun, state = await start(auth_env)
    issue = await auth_env.service.complete_login(
        "code", state, begun.browser_nonce.get_secret_value()
    )
    assert (
        await auth_env.service.resolve_session(issue.raw_token.get_secret_value())
    ).id == target.id


async def test_clock_rollback_does_not_accept_state_started_before_revoke(auth_env):
    await member(auth_env, role="admin")
    target = await member(auth_env, github_id=102, role="admin")
    actor = await auth_env.service.resolve_session(
        (await login(auth_env)).raw_token.get_secret_value()
    )
    auth_env.github_id = 102
    begun, state = await start(auth_env)
    await auth_env.service.update_member(actor, target.id, role="member", enabled=None)
    async with auth_env.factory.begin() as db:
        await db.execute(
            update(User)
            .where(User.id == target.id)
            .values(auth_changed_at=func.clock_timestamp() - timedelta(days=1))
        )
    with pytest.raises(AppError) as error:
        await auth_env.service.complete_login(
            "code", state, begun.browser_nonce.get_secret_value()
        )
    assert error.value.status == 403


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/repositories",
        "/admin/users",
        "/admin/audit",
        "/jobs/12345678-1234-1234-1234-123456789abc",
    ],
)
async def test_allowed_return_path_round_trips(auth_env, path):
    await member(auth_env)
    begun = await auth_env.service.begin_login(path)
    state = parse_qs(urlsplit(begun.authorize_url).query)["state"][0]
    issue = await auth_env.service.complete_login(
        "code", state, begun.browser_nonce.get_secret_value()
    )
    assert issue.return_path == path
