import logging

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
