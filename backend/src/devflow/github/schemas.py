"""Small, validated provider DTOs; token representations are always redacted."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class TokenSet(ProviderModel):
    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at: datetime | None
    refresh_expires_at: datetime | None


class InstallationToken(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: SecretStr
    expires_at: AwareDatetime
    permissions: dict[str, str]

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class GitHubUser(ProviderModel):
    id: PositiveId
    login: str
    avatar_url: str | None = None


class GitHubInstallationInfo(ProviderModel):
    id: PositiveId
    app_id: PositiveId
    account_id: PositiveId
    suspended_at: datetime | None


class GitHubRepository(ProviderModel):
    id: PositiveId
    full_name: str
    private: Annotated[bool, Field(strict=True)]
    default_branch: str | None = None


class UpstreamPage[T](ProviderModel):
    items: list[T]
    next_page: int | None
