"""Real routers and services; only the GitHub HTTP boundary is replaced."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest_asyncio
from cryptography.fernet import Fernet


@pytest_asyncio.fixture
async def auth_env(db_factory, migrated_database, workspace_tmp_path):
    from devflow.app import create_app
    from devflow.github.client import GitHubClient

    client_secret = workspace_tmp_path / "client-secret"
    client_secret.write_text("synthetic-client-secret")
    signing_key = workspace_tmp_path / "signing-key"
    signing_key.write_bytes(b"synthetic-signing-key-material-32bytes")
    cipher_key = workspace_tmp_path / "cipher-key"
    cipher_key.write_bytes(Fernet.generate_key())
    settings = migrated_database[0].model_copy(
        update={
            "github_client_id": "client-id",
            "github_client_secret_file": client_secret,
            "app_signing_key_file": signing_key,
            "credential_encryption_key_file": cipher_key,
        }
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
        env.app = create_app(settings, github=env.github)
        async with env.app.router.lifespan_context(env.app):
            env.cipher = env.app.state.cipher
            env.service = env.app.state.auth_service
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app),
        base_url=env.settings.public_base_url,
        follow_redirects=False,
    ) as client:
        yield client
