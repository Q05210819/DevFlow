"""Public authentication DTOs; raw secrets never serialize in clear text."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from devflow.auth.types import Role


class LoginStart(BaseModel):
    authorize_url: str
    browser_nonce: SecretStr


class SessionIssue(BaseModel):
    raw_token: SecretStr
    expires_at: datetime
    return_path: str


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    github_id: int
    login: str | None
    role: Role
    enabled: bool


class SessionView(BaseModel):
    user: UserView
    csrf_token: str


class MemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_id: Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
    role: Role = "member"


class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None = None
    enabled: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.role is None and self.enabled is None:
            raise ValueError("At least one change is required")
        return self


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actor_id: UUID | None
    action: str
    resource_type: str
    resource_id: UUID | None
    outcome: str
    request_id: UUID | None
    job_id: UUID | None
    reason_code: str | None
    created_at: datetime
