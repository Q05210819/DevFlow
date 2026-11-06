"""Validated runtime settings. Secrets stay in mounted files."""

import json
import os
from datetime import datetime
from decimal import Decimal
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
    worker_role: Literal["ordinary", "model"] = "ordinary"
    knowledge_tokenizer_path: Path | None = None
    model_embedding_path: Path | None = None
    model_reranker_path: Path | None = None
    model_lock_path: Path = Path(
        "C:/ProgramData/DevFlow/model-owner.lock"
        if os.name == "nt"
        else "/var/lib/devflow/model-owner.lock"
    )
    model_device: Literal["cpu", "cuda"] = "cpu"
    model_dtype: Literal["float16", "float32", "bfloat16"] = "float32"
    model_startup_timeout_seconds: float = Field(default=1800, gt=0, le=3600)
    model_drain_timeout_seconds: int = Field(default=120, gt=0, le=600)
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: SecretStr | None = None
    milvus_collection: str = Field(
        default="devflow_knowledge_v1",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,254}$",
    )
    milvus_timeout_seconds: float = Field(default=5, gt=0, le=5)
    milvus_build_timeout_seconds: int = Field(default=120, gt=0, le=600)
    sync_concurrency: int = Field(default=2, ge=1, le=10)
    sync_unit_requests: int = Field(default=5, ge=1, le=5)
    # Reserve the full 10-second HTTP deadline after up to 5 seconds of setup.
    sync_unit_seconds: int = Field(default=120, ge=15, le=120)
    sync_round_requests: int = Field(default=10000, ge=1, le=1000000)
    sync_round_seconds: int = Field(default=86400, ge=10, le=604800)
    sync_interval_seconds: int = Field(default=900, ge=1)
    sync_reconcile_seconds: int = Field(default=86400, ge=1)
    sync_history_days: int = Field(default=90, ge=1, le=365)
    sync_actions_days: int = Field(default=30, ge=1, le=365)
    sync_overlap_seconds: int = Field(default=300, ge=0, le=86400)

    # No price, currency or provider identity is invented in source. Cloud chat
    # stays disabled until a complete, explicit price sheet is configured.
    chat_cloud_enabled: bool = False
    chat_budget_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    chat_run_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    chat_month_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    chat_budget_minor_unit: Decimal | None = Field(
        default=None, gt=0, le=1, max_digits=6, decimal_places=6
    )
    chat_pricing_version: str | None = Field(default=None, pattern=r"^[!-~]{1,64}$")
    chat_pricing_valid_until: datetime | None = None
    chat_input_price_per_million: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    chat_output_price_per_million: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )

    workspace_git_executable: Path = Path(
        "C:/Program Files/Git/cmd/git.exe" if os.name == "nt" else "/usr/bin/git"
    )
    workspace_cache_root: Path = Path(
        "C:/ProgramData/DevFlow/workspace"
        if os.name == "nt"
        else "/var/lib/devflow/workspace"
    )
    workspace_repository_bytes: int = Field(default=2 * 1024**3, gt=0)
    workspace_total_bytes: int = Field(default=20 * 1024**3, gt=0)
    workspace_unused_days: int = Field(default=7, ge=1)
    workspace_orphan_grace_seconds: int = Field(default=600, ge=0, le=86400)

    @field_validator("workspace_git_executable", "workspace_cache_root")
    @classmethod
    def absolute_git_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("WORKSPACE_GIT_EXECUTABLE must be absolute")
        return value

    @field_validator("model_lock_path")
    @classmethod
    def absolute_model_lock_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("MODEL_LOCK_PATH must be absolute")
        return value

    @field_validator("model_embedding_path", "model_reranker_path")
    @classmethod
    def absolute_model_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Local model paths must be absolute")
        return value

    @field_validator("milvus_uri")
    @classmethod
    def milvus_http_origin(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("MILVUS_URI must be an HTTP(S) origin")
        return value.rstrip("/")

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

        # Knowledge cleanup holds the Job row for the whole 5-second fenced
        # transaction. A heartbeat at or below that fence can only time out on
        # the row lock, falsely report a lost lease and cancel the sweep.
        if self.job_heartbeat_seconds <= 5:
            raise ValueError(
                "JOB_HEARTBEAT_SECONDS must exceed the 5-second maintenance fence"
            )

        if self.worker_role == "model":
            if self.model_embedding_path is None or self.model_reranker_path is None:
                raise ValueError(
                    "A model worker requires local embedding and reranker paths"
                )
            # CPU is a valid local choice, but it (like every CUDA dtype) must
            # be an explicit deployment decision rather than a hidden default.
            missing = [
                name
                for name in ("model_device", "model_dtype")
                if name not in self.model_fields_set
            ]
            if missing:
                raise ValueError(
                    "MODEL_DEVICE and MODEL_DTYPE must be set explicitly for a "
                    "model worker"
                )

        price_sheet = (
            "chat_budget_currency",
            "chat_run_budget",
            "chat_month_budget",
            "chat_budget_minor_unit",
            "chat_pricing_version",
            "chat_pricing_valid_until",
            "chat_input_price_per_million",
            "chat_output_price_per_million",
        )
        if self.chat_cloud_enabled:
            if any(getattr(self, name) is None for name in price_sheet):
                raise ValueError(
                    "Cloud chat requires a complete currency and price sheet"
                )
            valid_until = self.chat_pricing_valid_until
            if (
                valid_until is None
                or valid_until.tzinfo is None
                or valid_until.utcoffset() is None
            ):
                raise ValueError("CHAT_PRICING_VALID_UNTIL must be timezone-aware")
        elif any(getattr(self, name) is not None for name in price_sheet):
            # A price sheet without the cloud mode is a mode mismatch, not a
            # harmless leftover: it can hide an intended-but-unauthorized call.
            raise ValueError("A chat price sheet requires CHAT_CLOUD_ENABLED=true")

        return self
