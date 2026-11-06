import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url


@pytest.fixture
def workspace_tmp_path():
    """Owned workspace fixtures retain inherited Windows sandbox permissions."""
    root = Path(__file__).resolve().parents[2] / ".superpowers" / "workspace-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    child = root / uuid4().hex
    child.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    # Retain evidence: process tests may fail before they can prove child teardown.
    return child


def pytest_asyncio_loop_factories():
    return {"selector": asyncio.SelectorEventLoop}


def validated_test_url(raw):
    url = make_url(raw)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.port != 55439
        or url.database != "devflow_test"
        or url.username != "devflow_test"
        or url.query
    ):
        raise ValueError("Tests require the dedicated loopback devflow_test database")
    return url


@pytest.fixture
def empty_database(settings_factory):
    url = validated_test_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://devflow_test:devflow_test@127.0.0.1:55439/devflow_test",
        )
    )
    schema = "test_" + uuid4().hex
    # The name is generated locally; never accept a user-controlled cleanup target.
    with psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
            settings = settings_factory(
                database_url=scoped.render_as_string(hide_password=False)
            )
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            config.attributes["database_url"] = settings.database_url.get_secret_value()
            yield settings, config
        finally:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def migrated_database(empty_database):
    settings, config = empty_database
    command.upgrade(config, "head")
    return settings, config


@pytest_asyncio.fixture
async def db_factory(migrated_database):
    from devflow.db import create_session_factory

    factory = create_session_factory(migrated_database[0])
    try:
        yield factory
    finally:
        await factory.kw["bind"].dispose()


@pytest.fixture
def settings_factory():
    def make_settings(**overrides):
        from devflow.config import Settings

        values = {
            "app_env": "test",
            "public_base_url": "http://localhost:3000",
            "database_url": "postgresql+psycopg://devflow:devflow@localhost/devflow",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return make_settings
