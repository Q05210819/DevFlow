from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from devflow.facts.schemas import StrictDTO

JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed"]
ORDINARY_JOB_KINDS = frozenset(
    {
        "repository.access_probe",
        "repository.sync",
        "repository.health",
        "repository.workspace",
        "knowledge.import",
        "knowledge.cleanup",
    }
)
MODEL_JOB_KINDS = frozenset(
    {
        "knowledge.index_batch",
        "knowledge.query",
        "chat.run",
    }
)
# Interactive model work is preferred, but background indexing must still make
# progress. The streak is per Queue/owner process and may reset on restart; Job
# deadlines, attempts and generation remain durable in PostgreSQL.
INTERACTIVE_MODEL_JOB_KINDS = ("knowledge.query", "chat.run")
MODEL_INTERACTIVE_STREAK_LIMIT = 10
MODEL_BACKGROUND_BATCH_CHUNKS = 16
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
    "workspace_busy",
    "workspace_not_found",
    "fork_source_unavailable",
    "git_failed",
    "cleanup_required",
    "storage_exhausted",
    "service_unavailable",
    "build_deadline_exceeded",
    "index_failed",
    "index_not_ready",
    "retrieval_unavailable",
    "context_limit",
    "state_limit",
    "answer_validation_failed",
    "model_not_configured",
    "model_call_outcome_unknown",
    "cancelled",
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


class KnowledgeImportInput(StrictDTO):
    import_id: UUID
    target_generation: int = Field(strict=True, ge=1)


class KnowledgeIndexInput(StrictDTO):
    """One durable batch Job for an immutable index candidate."""

    version: Literal[1]
    index_id: UUID


class ChatRunInput(StrictDTO):
    """Business references only; the question never enters the Job row."""

    version: Literal[1]
    run_id: UUID
    conversation_id: UUID
    message_id: UUID


class KnowledgeImportResult(StrictDTO):
    import_id: UUID
    target_generation: int = Field(strict=True, ge=1)
    status: Literal["building", "ready", "ready_partial"]


class KnowledgeCleanupInput(StrictDTO):
    """Finite maintenance input; the cursor is server-derived pagination."""

    version: Literal[1]
    limit: int = Field(strict=True, ge=1, le=100)
    after: UUID | None = None


class KnowledgeCleanupResult(StrictDTO):
    examined: int = Field(strict=True, ge=0, le=100)
    reclaimed: int = Field(strict=True, ge=0, le=100)
    deferred: int = Field(strict=True, ge=0, le=100)
    bytes_reclaimed: int = Field(strict=True, ge=0)
    chunks_reclaimed: int = Field(strict=True, ge=0)
    wrapped: bool = False
    cursor: UUID | None = None


class ChatCleanupInput(StrictDTO):
    """Finite terminal-thread sweep; candidates are always server-derived."""

    version: Literal[1]
    limit: int = Field(strict=True, ge=1, le=100)


class ChatCleanupResult(StrictDTO):
    """Checkpoint counters plus separately bounded content reclaim counts."""

    examined: int = Field(strict=True, ge=0, le=100)
    reclaimed: int = Field(strict=True, ge=0, le=100)
    bytes_reclaimed: int = Field(strict=True, ge=0)
    tool_calls_reclaimed: int = Field(default=0, strict=True, ge=0, le=100)
    messages_reclaimed: int = Field(default=0, strict=True, ge=0, le=100)
    receipts_reclaimed: int = Field(default=0, strict=True, ge=0, le=100)


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
