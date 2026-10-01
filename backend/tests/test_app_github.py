import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from devflow.app import create_app
from devflow.errors import AppError
from devflow.github.client import GitHubClient


def test_app_missing_optional_github_keeps_health(settings_factory):
    app = create_app(settings_factory(database_url=None))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert isinstance(app.state.github, GitHubClient)
        assert app.state.cipher is None and app.state.credential_service is None
        for path in (
            "/api/v1/session",
            "/api/v1/admin/users",
            "/api/v1/admin/audit-logs",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "unauthenticated"
        response = client.get("/api/v1/auth/github/start")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"


def test_app_injects_dependencies_and_does_not_close_external_http(
    migrated_database, tmp_path
):
    key = tmp_path / "fernet"
    key.write_bytes(Fernet.generate_key())
    settings = migrated_database[0].model_copy(
        update={"credential_encryption_key_file": key}
    )
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"id": 42, "login": "test"})
        )
    )
    github = GitHubClient(http, settings)
    app = create_app(settings, github=github)
    try:
        with TestClient(
            app, backend_options={"loop_factory": asyncio.SelectorEventLoop}
        ) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 200
            assert app.state.github is github
            assert (
                app.state.cipher is not None
                and app.state.credential_service is not None
            )
        assert not http.is_closed
    finally:
        asyncio.run(http.aclose())


def test_app_closes_only_internally_created_http(settings_factory, monkeypatch):
    original = httpx.AsyncClient
    clients = []

    def create_http(*args, **kwargs):
        client = original(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_http)
    app = create_app(settings_factory(database_url=None))
    with TestClient(app):
        assert len(clients) == 1 and not clients[0].is_closed
    assert clients[0].is_closed


def test_app_invalid_configured_cipher_fails_safely(settings_factory, tmp_path):
    key = tmp_path / "invalid-key"
    secret = "synthetic-invalid-fernet-key-sentinel"
    key.write_text(secret)
    app = create_app(
        settings_factory(database_url=None, credential_encryption_key_file=key)
    )
    with pytest.raises(AppError) as caught, TestClient(app):
        pass
    assert caught.value.code == "github_configuration_error"
    assert secret not in str(caught.value)
