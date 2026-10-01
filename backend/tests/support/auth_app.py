"""Real routers and services; only the GitHub HTTP boundary is replaced."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest_asyncio
from cryptography.fernet import Fernet


@pytest_asyncio.fixture
async def auth_env(db_factory, settings_factory, tmp_path):
    from devflow.auth.service import AuthService
    from devflow.github.client import GitHubClient
    from devflow.security import CredentialCipher

    client_secret = tmp_path / "client-secret"
    client_secret.write_text("synthetic-client-secret")
    signing_key = tmp_path / "signing-key"
    signing_key.write_bytes(b"synthetic-signing-key-material-32bytes")
    settings = settings_factory(
        github_client_id="client-id",
        github_client_secret_file=client_secret,
        app_signing_key_file=signing_key,
    )
    env = SimpleNamespace(
        factory=db_factory,
        settings=settings,
        github_id=101,
        login="renamed-user",
        before_user=None,
        token_status=200,
    )

    async def remote(request):
        if request.url.path == "/login/oauth/access_token":
            if env.token_status != 200:
                return httpx.Response(env.token_status, json={"error": "unavailable"})
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "expires_in": 3600,
                    "refresh_token_expires_in": 7200,
                    "token_type": "bearer",
                },
            )
        if request.url.path == "/user":
            if env.before_user:
                await env.before_user()
            return httpx.Response(200, json={"id": env.github_id, "login": env.login})
        raise AssertionError("Unexpected provider endpoint")

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        env.github = GitHubClient(http, settings)
        env.cipher = CredentialCipher(Fernet.generate_key())
        env.service = AuthService(db_factory, env.github, env.cipher, settings)
        yield env


async def member(env, github_id=101, role="member"):
    from devflow.auth.models import User

    async with env.factory.begin() as session:
        user = User(github_id=github_id, role=role)
        session.add(user)
        await session.flush()
        return user


async def login(env, return_path="/"):
    start = await env.service.begin_login(return_path)
    state = parse_qs(urlsplit(start.authorize_url).query)["state"][0]
    issue = await env.service.complete_login(
        "synthetic-code", state, start.browser_nonce.get_secret_value()
    )
    return issue


@asynccontextmanager
async def browser(env):
    from devflow.app import create_app
    from devflow.audit.routes import router as audit_router
    from devflow.auth.admin import router as admin_router
    from devflow.auth.routes import router

    app = create_app(env.settings)
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(audit_router)
    app.state.auth_service = env.service
    app.state.session_factory = env.factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=env.settings.public_base_url,
        follow_redirects=False,
    ) as client:
        yield client
