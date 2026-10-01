import asyncio
import ipaddress
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient


def test_live_does_not_call_github(monkeypatch, settings_factory):
    from devflow.app import create_app

    github_calls = []
    original_connect = socket.socket.connect

    def record_outbound(sock, address):
        if not ipaddress.ip_address(address[0]).is_loopback:
            github_calls.append(address)
            raise AssertionError(f"Unexpected network request to {address}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", record_outbound)
    try:
        with TestClient(create_app(settings_factory())) as client:
            response = client.get("/health/live")
    finally:
        monkeypatch.setattr(socket.socket, "connect", original_connect)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert github_calls == []


def test_request_id_is_unique_per_request(settings_factory):
    from devflow.app import create_app

    with TestClient(create_app(settings_factory())) as client:
        first = client.get("/health/live")
        second = client.get("/health/live")

    assert first.headers["x-request-id"]
    assert second.headers["x-request-id"]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_ready_detects_schema_mismatch(migrated_database):
    from sqlalchemy import create_engine, text

    from devflow.app import create_app

    settings, _ = migrated_database
    with TestClient(
        create_app(settings),
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as client:
        assert client.get("/health/ready").status_code == 200
        engine = create_engine(settings.database_url.get_secret_value())
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE alembic_version SET version_num = 'wrong'")
                )
        finally:
            engine.dispose()
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200


@pytest.mark.parametrize(
    "url", [None, "postgresql+psycopg://bad:secret@127.0.0.1:1/missing"]
)
def test_ready_reports_connection_failure(settings_factory, url):
    from devflow.app import create_app

    with TestClient(create_app(settings_factory(database_url=url))) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "secret" not in response.text
        assert client.get("/health/live").status_code == 200


def test_documented_server_connects_to_postgres(migrated_database):
    root = Path(__file__).resolve().parents[2]
    launch = next(
        line
        for line in (root / "README.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("uv --directory backend run --env-file ")
    )
    parts = shlex.split(launch)
    args = [sys.executable, "-m", "uvicorn", *parts[parts.index("uvicorn") + 1 :]]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    args[args.index("--port") + 1] = str(port)
    env = dict(
        os.environ,
        APP_ENV="test",
        PUBLIC_BASE_URL="http://localhost:3000",
        DATABASE_URL=migrated_database[0].database_url.get_secret_value(),
    )
    process = subprocess.Popen(
        args,
        cwd=root / "backend",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    status = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/health/ready", timeout=1
                ) as response:
                    status = response.status
                break
            except URLError as exc:
                if getattr(exc, "code", None):
                    status = exc.code
                    break
                time.sleep(0.1)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    assert status == 200, (stdout, stderr)
