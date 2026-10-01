"""Production factory wiring across login, authorization, and audit routes."""

from urllib.parse import parse_qs, urlsplit

import pytest
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, member

pytestmark = pytest.mark.asyncio


async def test_factory_routes_share_lifespan_services_and_database(auth_env):
    await member(auth_env, role="admin")
    app = auth_env.app
    assert app.state.auth_service is auth_env.service
    assert app.state.auth_service.factory is app.state.session_factory
    assert app.state.credential_service.factory is app.state.session_factory
    assert app.state.github is auth_env.github
    assert app.state.cipher is auth_env.cipher

    expected = (
        "/api/v1/auth/github/start",
        "/api/v1/auth/github/callback",
        "/api/v1/session",
        "/api/v1/auth/logout",
        "/api/v1/admin/users",
        "/api/v1/admin/users/{user_id}",
        "/api/v1/admin/audit-logs",
    )
    assert set(expected).issubset(app.openapi()["paths"])

    async with browser(auth_env) as client:
        begun = await client.get("/api/v1/auth/github/start")
        assert begun.status_code == 302
        state = parse_qs(urlsplit(begun.headers["location"]).query)["state"][0]
        done = await client.get(
            "/api/v1/auth/github/callback", params={"state": state, "code": "code"}
        )
        assert done.status_code == 303
        current = await client.get("/api/v1/session")
        assert current.status_code == 200
        assert current.json()["user"]["role"] == "admin"
        assert (await client.get("/api/v1/admin/users")).status_code == 200
        audit = await client.get("/api/v1/admin/audit-logs")
        assert audit.status_code == 200
        assert audit.json()["items"]
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": auth_env.settings.public_base_url,
                "X-CSRF-Token": current.json()["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert (await client.get("/api/v1/session")).status_code == 401
