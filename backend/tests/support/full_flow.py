"""Acceptance fixtures: real application state, synthetic GitHub HTTP only."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from devflow.config import Settings

ADMIN_ID = 41001
MEMBER_ID = 42002
INSTALLATION_ID = 11001
REPOSITORY_ID = 101001
REPOSITORY_NAME = "acceptance/private-repository"


def synthetic_settings(directory: Path, database_url: str, origin: str) -> Settings:
    """Use ordinary settings with ephemeral, explicitly synthetic secrets."""
    directory.mkdir(parents=True, exist_ok=True)
    secrets = {
        "github_client_secret_file": b"synthetic-acceptance-client-secret",
        "github_webhook_secret_file": b"synthetic-acceptance-webhook-secret",
        "app_signing_key_file": b"synthetic-acceptance-signing-key-32-bytes",
        "credential_encryption_key_file": Fernet.generate_key(),
        "github_private_key_file": rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    }
    paths = {}
    for name, value in secrets.items():
        paths[name] = directory / name
        paths[name].write_bytes(value)
    return Settings(
        _env_file=None,
        app_env="test",
        public_base_url=origin,
        database_url=database_url,
        bootstrap_admin_github_id=ADMIN_ID,
        github_app_id=7001,
        github_client_id="synthetic-acceptance-client",
        github_allowed_installation_ids=[INSTALLATION_ID],
        **paths,
    )


class SyntheticGitHub:
    """Codes select a synthetic identity; production OAuth still validates state."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "github.com" and path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            code = form.get("code", [""])[0]
            if code not in {"acceptance-admin", "acceptance-member"}:
                return httpx.Response(400, json={"error": "bad_verification_code"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"synthetic-{code}",
                    "refresh_token": f"synthetic-refresh-{code}",
                    "expires_in": 3600,
                    "refresh_token_expires_in": 7200,
                    "token_type": "bearer",
                },
            )
        assert request.url.host == "api.github.com"
        assert request.method == "GET"
        installation = {
            "id": INSTALLATION_ID,
            "app_id": 7001,
            "account": {"id": 99001},
            "suspended_at": None,
        }
        if path == f"/app/installations/{INSTALLATION_ID}":
            return httpx.Response(200, json=installation)
        token = request.headers.get("authorization")
        assert token in {
            "Bearer synthetic-acceptance-admin",
            "Bearer synthetic-acceptance-member",
        }
        if path == "/user":
            admin = token == "Bearer synthetic-acceptance-admin"
            return httpx.Response(
                200,
                json={
                    "id": ADMIN_ID if admin else MEMBER_ID,
                    "login": "acceptance-admin" if admin else "acceptance-member",
                },
            )
        assert request.url.params["per_page"] == "100"
        assert request.url.params["page"] == "1"
        if path == "/user/installations":
            return httpx.Response(200, json={"installations": [installation]})
        if path == f"/user/installations/{INSTALLATION_ID}/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": REPOSITORY_ID,
                            "full_name": REPOSITORY_NAME,
                            "private": True,
                            "default_branch": "main",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected synthetic GitHub endpoint: {path}")


async def oauth_login(client: httpx.AsyncClient, identity: str) -> dict:
    start = await client.get("/api/v1/auth/github/start")
    assert start.status_code == 302
    assert client.cookies.get("devflow_oauth")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await client.get(
        "/api/v1/auth/github/callback",
        params={"code": f"acceptance-{identity}", "state": state},
    )
    assert callback.status_code == 303, callback.text
    assert client.cookies.get("devflow_session")
    response = await client.get("/api/v1/session")
    assert response.status_code == 200, response.text
    return response.json()


def csrf_headers(session: dict, origin: str) -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": session["csrf_token"]}
