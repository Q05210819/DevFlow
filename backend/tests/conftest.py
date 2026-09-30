import pytest


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
