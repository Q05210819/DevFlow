from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from devflow.facts.schemas import StrictDTO

JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed"]
JobErrorCode = Literal[
    "unauthenticated",
    "resource_not_found",
    "validation_error",
    "reauth_required",
    "github_rate_limited",
    "github_unavailable",
    "github_configuration_error",
    "attempt_timeout",
    "permission_revoked",
    "lease_expired",
    "internal_error",
    "permission_missing",
    "github_installation_unavailable",
    "github_response_too_large",
    "version_changed",
    "version_unavailable",
    "source_limit",
    "budget_exhausted",
]
IdempotencyKey = Annotated[str, Field(strict=True, pattern=r"^[!-~]{1,128}$")]


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    github_repository_id: int
    full_name: str
    private: bool
    default_branch: str | None
    checked_at: datetime


class SyncResult(StrictDTO):
    repository_id: UUID
    generation: int = Field(strict=True, ge=1)
    continued: bool = Field(strict=True)


class HealthResult(StrictDTO):
    snapshot_id: UUID
    generation: int = Field(strict=True, ge=1)


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: JobStatus
    attempt: int = Field(ge=0, le=3)
    error_code: JobErrorCode | None = None


class EventView(EventPayload):
    sequence: int = Field(ge=1)


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: UUID
    repository_id: UUID
    requested_by: UUID
    status: JobStatus
    attempt: int = Field(ge=0, le=3)
    max_attempts: int = Field(ge=1, le=3)
    result: ProbeResult | None
    error_code: JobErrorCode | None
    last_sequence: int
    created_at: datetime
    updated_at: datetime
