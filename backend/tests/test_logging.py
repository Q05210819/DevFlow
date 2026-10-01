import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient


def test_error_and_log_redaction(caplog, settings_factory):
    from devflow.app import create_app
    from devflow.errors import AppError

    sentinel_secrets = ["oauth-code-sentinel", "cookie-sentinel", "bearer-sentinel"]
    app = create_app(settings_factory())

    @app.get("/_failure")
    def failure():
        raise AppError("github_unavailable", 503, "GitHub is unavailable")

    with caplog.at_level(logging.INFO, logger="devflow"), TestClient(app) as client:
        response = client.get(
            "/_failure?code=oauth-code-sentinel",
            headers={
                "Cookie": "session=cookie-sentinel",
                "Authorization": "Bearer bearer-sentinel",
            },
        )

    output = response.text + "\n" + caplog.text
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "github_unavailable",
            "message": "GitHub is unavailable",
            "request_id": response.headers["x-request-id"],
        }
    }
    assert all(secret not in output for secret in sentinel_secrets)
    assert any("request_id" in record.message for record in caplog.records)


def test_validation_error_does_not_echo_input(settings_factory):
    from devflow.app import create_app

    app = create_app(settings_factory())

    @app.get("/_number")
    def number(value: int):
        return {"value": value}

    with TestClient(app) as client:
        response = client.get("/_number?value=private-sentinel")

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Invalid request",
            "request_id": response.headers["x-request-id"],
        }
    }
    assert "private-sentinel" not in response.text


def test_request_is_logged_without_test_logging_setup(capsys, settings_factory):
    from devflow.app import create_app

    with TestClient(create_app(settings_factory())) as client:
        response = client.get("/health/live")

    assert response.headers["x-request-id"] in capsys.readouterr().err


def test_documented_server_does_not_log_request_secrets():
    root = Path(__file__).resolve().parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    launch = next(
        line
        for line in readme.splitlines()
        if line.startswith("uv --directory backend run --env-file ")
    )
    launch_parts = shlex.split(launch)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        *launch_parts[launch_parts.index("uvicorn") + 1 :],
    ]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command[command.index("--port") + 1] = str(port)
    environment = os.environ.copy()
    environment["APP_ENV"] = "test"
    environment["PUBLIC_BASE_URL"] = "http://127.0.0.1:3000"
    process = subprocess.Popen(
        command,
        cwd=root / "backend",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    response = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                request = Request(
                    f"http://127.0.0.1:{port}/health/live?code=oauth-code-sentinel",
                    headers={
                        "Cookie": "session=cookie-sentinel",
                        "Authorization": "Bearer bearer-sentinel",
                    },
                )
                with urlopen(request, timeout=1) as result:
                    response = result.status
                break
            except URLError:
                time.sleep(0.1)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()

    assert response == 200, (stdout, stderr)
    output = stdout + stderr
    assert "oauth-code-sentinel" not in output
    assert "cookie-sentinel" not in output
    assert "bearer-sentinel" not in output


def test_unexpected_error_has_matching_request_id_and_safe_log(
    caplog, settings_factory
):
    from devflow.app import create_app

    app = create_app(settings_factory())

    @app.get("/_unexpected")
    def unexpected():
        raise RuntimeError("private-exception-sentinel")

    with (
        caplog.at_level(logging.INFO, logger="devflow"),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/_unexpected?code=oauth-code-sentinel")

    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": request_id,
        }
    }
    assert any(
        f'"request_id":"{request_id}"' in record.message
        and '"status_code":500' in record.message
        for record in caplog.records
    )
    output = response.text + caplog.text
    assert "private-exception-sentinel" not in output
    assert "oauth-code-sentinel" not in output
