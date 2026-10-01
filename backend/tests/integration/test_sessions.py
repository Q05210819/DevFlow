import base64
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member

from devflow.auth.models import Session
from devflow.errors import AppError

pytestmark = pytest.mark.asyncio


async def test_session_expires_and_logout_invalidates(auth_env):
    await member(auth_env)
    issue = await login(auth_env)
    raw = issue.raw_token.get_secret_value()
    assert len(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) == 32
    assert (await auth_env.service.resolve_session(raw)).github_id == 101
    async with auth_env.factory.begin() as db:
        row = await db.scalar(select(Session))
        assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert row.expires_at - row.created_at == timedelta(hours=8)
        await db.execute(update(Session).values(expires_at=func.clock_timestamp()))
    with pytest.raises(AppError) as error:
        await auth_env.service.resolve_session(raw)
    assert error.value.status == 401
    second = await login(auth_env)
    await auth_env.service.logout(second.raw_token.get_secret_value())
    with pytest.raises(AppError) as error:
        await auth_env.service.resolve_session(second.raw_token.get_secret_value())
    assert error.value.status == 401


async def test_no_cookie_is_401_even_when_oauth_not_configured(auth_env):
    auth_env.settings.github_client_id = None
    async with browser(auth_env) as client:
        for path in [
            "/api/v1/session",
            "/api/v1/admin/users",
            "/api/v1/admin/audit-logs",
        ]:
            response = await client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthenticated"
        response = await client.get("/api/v1/auth/github/start")
        assert response.status_code == 503


async def test_session_dto_and_logout_cookie(auth_env):
    await member(auth_env)
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        response = await client.get("/api/v1/session")
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"user", "csrf_token"}
        assert set(payload["user"]) == {"id", "github_id", "login", "role", "enabled"}
        response = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": payload["csrf_token"],
            },
        )
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert (await client.get("/api/v1/session")).status_code == 401


async def test_production_cookie_properties(auth_env):
    from urllib.parse import parse_qs, urlsplit

    await member(auth_env)
    auth_env.settings.app_env = "production"
    auth_env.settings.public_base_url = "https://devflow.example"
    async with browser(auth_env) as client:
        begun = await client.get("/api/v1/auth/github/start")
        nonce_cookie = begun.headers["set-cookie"]
        assert "__Host-devflow_oauth=" in nonce_cookie
        assert "Secure" in nonce_cookie and "HttpOnly" in nonce_cookie
        state = parse_qs(urlsplit(begun.headers["location"]).query)["state"][0]
        done = await client.get(
            "/api/v1/auth/github/callback", params={"state": state, "code": "code"}
        )
        assert done.status_code == 303
        cookie = done.headers.get_list("set-cookie")[0]
        assert cookie.startswith("__Host-devflow_session=")
        for attribute in [
            "HttpOnly",
            "Secure",
            "SameSite=lax",
            "Path=/",
            "Max-Age=28800",
        ]:
            assert attribute in cookie
        assert "Domain=" not in cookie


@pytest.mark.parametrize("key", [None, b"", b"too-short"])
async def test_signing_key_missing_or_invalid_prevents_login(auth_env, key):
    await member(auth_env)
    if key is None:
        auth_env.settings.app_signing_key_file = None
    else:
        auth_env.settings.app_signing_key_file.write_bytes(key)
    async with browser(auth_env) as client:
        response = await client.get("/api/v1/auth/github/start")
        assert response.status_code == 503
        assert (await client.get("/api/v1/session")).status_code == 401
    async with auth_env.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
