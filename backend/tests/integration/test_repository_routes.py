import pytest
from sqlalchemy import update
from support.repository_env import browser, repository
from support.repository_env import repo_env as repo_env  # noqa: PLC0414


@pytest.mark.asyncio
async def test_repository_routes_require_authentication(repo_env):
    async with browser(repo_env) as (client, _app, _raw):
        for path in ["/api/v1/repositories", "/api/v1/github/repositories"]:
            response = await client.get(path)
            assert response.status_code == 401
        response = await client.post(
            "/api/v1/repositories",
            json={"installation_id": 11, "github_repository_id": 101},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_actual_factory_register_discover_and_list(repo_env):
    async with browser(repo_env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        response = await client.get("/api/v1/github/repositories")
        assert response.status_code == 200
        assert response.json()["items"][0]["github_repository_id"] == 101
        assert response.headers["cache-control"] == "no-store"
        body = {"installation_id": 11, "github_repository_id": 101}
        assert (await client.post("/api/v1/repositories", json=body)).status_code == 403
        csrf = app.state.auth_service.session_csrf(raw)
        headers = {"Origin": repo_env.settings.public_base_url, "X-CSRF-Token": csrf}
        first = await client.post("/api/v1/repositories", json=body, headers=headers)
        second = await client.post("/api/v1/repositories", json=body, headers=headers)
        assert first.status_code == 201 and second.status_code == 200
        assert first.json() == second.json()
        assert set(first.json()) == {
            "id",
            "github_repository_id",
            "installation_id",
            "full_name",
            "private",
            "default_branch",
        }
        response = await client.get("/api/v1/repositories")
        item = response.json()["items"][0]
        assert item.pop("sync")["status"] == "queued"
        assert [item] == [first.json()]


@pytest.mark.asyncio
async def test_repository_registration_member_and_numeric_guards(repo_env):
    from devflow.auth.models import User

    env = repo_env
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": app.state.auth_service.session_csrf(raw),
        }
        for value in [True, "101", -1, 0, 2**63]:
            response = await client.post(
                "/api/v1/repositories",
                json={"installation_id": 11, "github_repository_id": value},
                headers=headers,
            )
            assert response.status_code == 422
        async with env.factory.begin() as session:
            await session.execute(
                update(User).where(User.id == env.actor.id).values(role="member")
            )
        response = await client.post(
            "/api/v1/repositories",
            json={"installation_id": 11, "github_repository_id": 101},
            headers=headers,
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_with_no_user_repository_gets_404(repo_env):
    env = repo_env
    env.repositories = {}
    async with browser(env) as (client, app, raw):
        client.cookies.set("devflow_session", raw)
        headers = {
            "Origin": env.settings.public_base_url,
            "X-CSRF-Token": app.state.auth_service.session_csrf(raw),
        }
        response = await client.post(
            "/api/v1/repositories",
            json={"installation_id": 11, "github_repository_id": 101},
            headers=headers,
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_http_cursor_rejects_cross_user_query_and_tampering(repo_env):
    import hashlib

    from sqlalchemy import func, text

    from devflow.auth.models import Session, User

    env = repo_env
    env.repositories[(11, 1)] = ([repository(101), repository(102)], None)
    async with browser(env) as (client, _app, raw):
        client.cookies.set("devflow_session", raw)
        first = await client.get("/api/v1/github/repositories", params={"limit": 1})
        cursor = first.json()["next_cursor"]
        for token, limit in [(cursor, 2), ("A" + cursor[1:], 1)]:
            response = await client.get(
                "/api/v1/github/repositories", params={"limit": limit, "cursor": token}
            )
            assert response.status_code == 400 and "items" not in response.json()
        async with env.factory.begin() as session:
            user = User(github_id=43, role="member")
            session.add(user)
            await session.flush()
            session.add(
                Session(
                    user_id=user.id,
                    token_hash=hashlib.sha256(b"second-session").hexdigest(),
                    auth_version=1,
                    expires_at=func.clock_timestamp() + text("interval '8 hours'"),
                )
            )
        client.cookies.set("devflow_session", "second-session")
        response = await client.get(
            "/api/v1/github/repositories", params={"limit": 1, "cursor": cursor}
        )
        assert response.status_code == 400 and "items" not in response.json()
