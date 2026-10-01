"""Real database/services with only GitHub's HTTP transport replaced."""

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest_asyncio
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr


def installation(number=11, app=7):
    return {"id": number, "app_id": app, "account": {"id": 99}, "suspended_at": None}


def repository(number=101, name="team/private"):
    return {"id": number, "full_name": name, "private": True, "default_branch": "main"}


@pytest_asyncio.fixture
async def repo_env(db_factory, settings_factory, tmp_path):
    from devflow.auth.models import User
    from devflow.auth.types import Actor
    from devflow.github.client import GitHubClient
    from devflow.github.credentials import CredentialService
    from devflow.github.models import GitHubCredential
    from devflow.repositories.policy import RepositoryPolicy
    from devflow.repositories.service import RepositoryService
    from devflow.security import CredentialCipher

    private = tmp_path / "private"
    private.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signing = tmp_path / "signing"
    signing.write_bytes(b"s" * 32)
    encryption = tmp_path / "encryption"
    encryption.write_bytes(Fernet.generate_key())
    settings = settings_factory(
        database_url=db_factory.kw["bind"].url.render_as_string(hide_password=False),
        github_app_id=7,
        github_client_id="client",
        github_private_key_file=private,
        github_allowed_installation_ids=[11, 12],
        app_signing_key_file=signing,
        credential_encryption_key_file=encryption,
    )
    env = SimpleNamespace(
        factory=db_factory,
        settings=settings,
        calls=[],
        installations={1: ([installation()], None)},
        repositories={(11, 1): ([repository()], None)},
        status=200,
        before_response=None,
        app_status=200,
    )

    async def remote(request):
        env.calls.append(request)
        path = request.url.path
        if env.before_response:
            await env.before_response(request)
        if path.startswith("/app/installations/"):
            return httpx.Response(
                env.app_status, json=installation(int(path.rsplit("/", 1)[1]))
            )
        assert request.method == "GET"
        assert request.url.params["per_page"] == "100"
        if env.status != 200:
            return httpx.Response(env.status, json={"message": "provider error"})
        page = int(request.url.params["page"])
        if path == "/user/installations":
            items, following = env.installations.get(page, ([], None))
            key = "installations"
        else:
            assert path.startswith("/user/installations/") and path.endswith(
                "/repositories"
            )
            items, following = env.repositories.get(
                (int(path.split("/")[3]), page), ([], None)
            )
            key = "repositories"
        headers = (
            {"Link": f'<https://api.github.com{path}?page={following}>; rel="next"'}
            if following
            else {}
        )
        return httpx.Response(200, json={key: items}, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as http:
        env.github = GitHubClient(http, settings)
        env.cipher = CredentialCipher.from_file(encryption)
        env.credentials = CredentialService(db_factory, env.github, env.cipher)
        async with db_factory.begin() as session:
            user = User(github_id=42, role="admin")
            session.add(user)
            await session.flush()
            session.add(
                GitHubCredential(
                    user_id=user.id,
                    access_token_encrypted=env.cipher.encrypt(SecretStr("user-token")),
                )
            )
            env.actor = Actor(user.id, user.github_id, user.role, user.auth_version)
        env.policy = RepositoryPolicy(db_factory, env.github, env.credentials, settings)
        env.service = RepositoryService(
            db_factory, env.github, env.credentials, settings, policy=env.policy
        )
        yield env


@asynccontextmanager
async def browser(env):
    from sqlalchemy import func, text

    from devflow.app import create_app
    from devflow.auth.models import Session

    raw = "synthetic-browser-session"
    async with env.factory.begin() as session:
        session.add(
            Session(
                user_id=env.actor.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                auth_version=env.actor.auth_version,
                expires_at=func.clock_timestamp() + text("interval '8 hours'"),
            )
        )
    app = create_app(env.settings, github=env.github)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=env.settings.public_base_url,
        ) as client,
    ):
        yield client, app, raw
