"""Small, validated provider DTOs; token representations are always redacted."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class TokenSet(ProviderModel):
    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at: datetime | None
    refresh_expires_at: datetime | None


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
