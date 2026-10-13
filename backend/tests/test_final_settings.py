import os
import subprocess
import sys

import psycopg
import pytest
from conftest import empty_database, validated_test_url
from pydantic import ValidationError


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"APP_ENV": "production"}, "HTTP PUBLIC_BASE_URL requires"),
        ({"APP_ENV": "invalid"}, "Input should be"),
        ({"PUBLIC_BASE_URL": None}, "Field required"),
    ],
)
def test_uvicorn_startup_validation_hides_inputs(overrides, message):
    secret = "final-pw-42"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    env.update(
        APP_ENV="test",
        PUBLIC_BASE_URL="http://localhost",
        DATABASE_URL=f"postgresql+psycopg://u:{secret}@h/d",
    )
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "devflow.app:create_app",
            "--factory",
            "--no-access-log",
            "--loop",
            "asyncio:SelectorEventLoop",
        ],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 1
    assert "ValidationError" in result.stderr
    assert message in result.stderr
    # Boolean assertion deliberately avoids rendering captured secret input on RED.
    leaked = any(secret in output for output in (result.stdout, result.stderr))
    assert not leaked


@pytest.mark.parametrize(
    "setting,value",
    [
        ("oauth_state_ttl_seconds", 599),
        ("session_ttl_seconds", 28799),
        ("permission_cache_ttl_seconds", -1),
        ("permission_cache_ttl_seconds", 61),
    ],
)
def test_auth_lifetimes_and_cache_bounds(settings_factory, setting, value):
    with pytest.raises(ValidationError):
        settings_factory(**{setting: value})


@pytest.mark.parametrize("module", ["devflow.worker", "devflow.bootstrap"])
def test_other_startup_paths_hide_settings_inputs(module):
    secret = "final-loader-pw"
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    env.update(
        APP_ENV="production",
        PUBLIC_BASE_URL="http://localhost",
        DATABASE_URL=f"postgresql+psycopg://u:{secret}@h/d",
    )
    result = subprocess.run(
        [sys.executable, "-m", module],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 1
    leaked = any(secret in output for output in (result.stdout, result.stderr))
    assert not leaked


def test_owned_schema_cleanup_when_settings_setup_fails():
    created = []

    def fail_settings(**values):
        from sqlalchemy.engine import make_url

        created.append(make_url(values["database_url"]).query["options"].split("=")[1])
        raise RuntimeError("synthetic setup failure")

    fixture = empty_database.__wrapped__(fail_settings)
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        next(fixture)
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as conn:
        try:
            assert (
                conn.execute(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = %s",
                    (created[0],),
                ).fetchone()[0]
                == 0
            )
        finally:
            # Cleanup even on the intentionally failing pre-fix run.
            assert created[0].startswith("test_") and len(created[0]) == 37
            conn.execute(f'DROP SCHEMA IF EXISTS "{created[0]}" CASCADE')
