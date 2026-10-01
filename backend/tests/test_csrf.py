import pytest
from support.auth_app import auth_env as auth_env  # noqa: PLC0414
from support.auth_app import browser, login, member

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://localhost:3000.evil.test",
        "http://localhost:3000/",
        "https://localhost:3000",
    ],
)
async def test_writes_require_exact_origin(auth_env, origin):
    await member(auth_env)
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        if origin is not None:
            headers["Origin"] = origin
        response = await client.post("/api/v1/auth/logout", headers=headers)
        assert response.status_code == 403
        assert (await client.get("/api/v1/session")).status_code == 200


async def test_csrf_missing_wrong_or_other_session_rejected(auth_env):
    await member(auth_env)
    first, second = await login(auth_env), await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", first.raw_token.get_secret_value())
        foreign_csrf = (await client.get("/api/v1/session")).json()["csrf_token"]
        client.cookies.set("devflow_session", second.raw_token.get_secret_value())
        for token in [None, "wrong", foreign_csrf, "non-ascii-ü"]:
            headers = {"Origin": "http://localhost:3000"}
            if token:
                headers["X-CSRF-Token"] = (
                    token if token.isascii() else b"non-ascii-\xff"
                )
            response = await client.post("/api/v1/auth/logout", headers=headers)
            assert response.status_code == 403


async def test_member_write_requires_csrf(auth_env):
    await member(auth_env, role="admin")
    issue = await login(auth_env)
    async with browser(auth_env) as client:
        client.cookies.set("devflow_session", issue.raw_token.get_secret_value())
        response = await client.post(
            "/api/v1/admin/users",
            json={"github_id": 102},
            headers={"Origin": "http://localhost:3000"},
        )
        assert response.status_code == 403
