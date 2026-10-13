"""Validated runtime settings. Secrets stay in mounted files."""

import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

InstallationId = Annotated[int, Field(strict=True, gt=0)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    app_env: Literal["development", "test", "production"]
    public_base_url: str
    database_url: SecretStr | None = None

    github_app_id: int | None = Field(default=None, gt=0)
    github_client_id: str | None = None
    github_allowed_installation_ids: list[InstallationId] = Field(default_factory=list)
    bootstrap_admin_github_id: int | None = Field(default=None, gt=0)

    github_client_secret_file: Path | None = None
    github_private_key_file: Path | None = None
    github_webhook_secret_file: Path | None = None
    credential_encryption_key_file: Path | None = None
    app_signing_key_file: Path | None = None

    oauth_state_ttl_seconds: int = Field(default=600, ge=600, le=600)
    session_ttl_seconds: int = Field(default=28800, ge=28800, le=28800)
    permission_cache_ttl_seconds: int = Field(default=60, ge=0, le=60)
    job_max_attempts: int = Field(default=3, gt=0, le=3)
    job_retry_first_seconds: int = Field(default=10, gt=0)
    job_retry_second_seconds: int = Field(default=30, gt=0)
    job_lease_seconds: int = Field(default=30, gt=0)
    job_heartbeat_seconds: int = Field(default=10, gt=0)
    job_attempt_timeout_seconds: int = Field(default=120, gt=0)
    worker_concurrency: int = Field(default=4, gt=0)

    @field_validator("github_allowed_installation_ids", mode="before")
    @classmethod
    def parse_allowed_installations(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value)
        return value

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        url = urlsplit(self.public_base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin")

        if url.scheme == "http" and (
            self.app_env == "production"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "HTTP PUBLIC_BASE_URL requires a development/test loopback host"
            )

        if self.app_env == "production":
            required = (
                "database_url",
                "github_app_id",
                "github_client_id",
                "bootstrap_admin_github_id",
                "github_allowed_installation_ids",
            )
            if any(not getattr(self, name) for name in required):
                raise ValueError(
                    "Production identity and database settings are required"
                )

            secret_files = (
                "github_client_secret_file",
                "github_private_key_file",
                "github_webhook_secret_file",
                "credential_encryption_key_file",
                "app_signing_key_file",
            )
            for name in secret_files:
                path = getattr(self, name)
                if path is None or not path.is_file() or not path.read_bytes().strip():
                    raise ValueError(
                        f"Production secret file {name.upper()} is required"
                    )

        return self
