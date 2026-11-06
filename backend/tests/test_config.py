import pytest
from pydantic import SecretStr, ValidationError


def test_production_rejects_http_or_missing_secrets(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(app_env="production", public_base_url="http://localhost:3000")

    with pytest.raises(ValidationError):
        settings_factory(
            app_env="production",
            public_base_url="https://devflow.example",
            github_app_id=1,
            github_client_id="client-id",
            github_allowed_installation_ids=[2],
            bootstrap_admin_github_id=3,
        )


def test_development_http_requires_loopback(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(
            app_env="development", public_base_url="http://devflow.example:3000"
        )

    settings = settings_factory(
        app_env="development", public_base_url="http://127.0.0.1:3000"
    )
    assert settings.public_base_url == "http://127.0.0.1:3000"


def test_public_base_url_rejects_path(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(public_base_url="https://devflow.example/app")


def test_allowed_installations_parse_json_integer_array(settings_factory):
    settings = settings_factory(github_allowed_installation_ids="[42, 99]")
    assert settings.github_allowed_installation_ids == [42, 99]

    with pytest.raises(ValidationError):
        settings_factory(github_allowed_installation_ids='["42"]')


def test_database_url_is_secret(settings_factory):
    settings = settings_factory(
        database_url="postgresql+psycopg://user:password-sentinel@localhost/devflow"
    )
    assert isinstance(settings.database_url, SecretStr)
    assert "password-sentinel" not in repr(settings)


def test_production_rejects_empty_secret_file(settings_factory, workspace_tmp_path):
    names = (
        "github_client_secret_file",
        "github_private_key_file",
        "github_webhook_secret_file",
        "credential_encryption_key_file",
        "app_signing_key_file",
    )
    paths = {}
    for name in names:
        path = workspace_tmp_path / name
        path.write_text("  \n" if name == "app_signing_key_file" else "sample-value")
        paths[name] = path

    with pytest.raises(ValidationError):
        settings_factory(
            app_env="production",
            public_base_url="https://devflow.example",
            github_app_id=1,
            github_client_id="client-id",
            github_allowed_installation_ids=[2],
            bootstrap_admin_github_id=3,
            **paths,
        )


def test_heartbeat_must_exceed_maintenance_fence(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(job_heartbeat_seconds=5)
    assert settings_factory(job_heartbeat_seconds=6).job_heartbeat_seconds == 6


def test_model_worker_requires_explicit_device_dtype_and_paths(
    settings_factory, workspace_tmp_path
):
    embedding = workspace_tmp_path / "embedding"
    reranker = workspace_tmp_path / "reranker"
    embedding.mkdir()
    reranker.mkdir()
    selected = {
        "worker_role": "model",
        "model_device": "cpu",
        "model_dtype": "float32",
    }
    with pytest.raises(ValidationError, match="local embedding and reranker"):
        settings_factory(**selected)
    with pytest.raises(ValidationError, match="must be set explicitly"):
        settings_factory(
            worker_role="model",
            model_embedding_path=embedding,
            model_reranker_path=reranker,
        )
    settings = settings_factory(
        **selected,
        model_embedding_path=embedding,
        model_reranker_path=reranker,
    )
    assert settings.worker_role == "model"
    assert settings.model_device == "cpu"
    assert settings.model_dtype == "float32"
