from contextvars import ContextVar
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from devflow.audit.models import AuditLog

request_id_context: ContextVar[UUID | None] = ContextVar(
    "audit_request_id", default=None
)

AuditAction = Literal[
    "auth.login",
    "auth.logout",
    "member.created",
    "member.updated",
    "bootstrap.admin",
    "repository.registered",
    "repository.invalidated",
    "permission.denied",
    "webhook.verified",
    "webhook.processed",
    "credential.revoked",
    "job.submitted",
    "job.started",
    "job.retried",
    "job.succeeded",
    "job.failed",
    "sync.requested",
    "backfill.requested",
    "health.requested",
    "workspace.requested",
    "chat.budget.reconciliation",
]
ReasonCode = Literal[
    "unauthenticated",
    "forbidden",
    "resource_not_found",
    "validation_error",
    "idempotency_conflict",
    "github_rate_limited",
    "github_unavailable",
    "github_configuration_error",
    "reauth_required",
    "attempt_timeout",
    "last_admin",
    "invalid_state",
    "state_expired",
    "state_consumed",
    "browser_mismatch",
    "member_not_allowed",
    "member_disabled",
    "invalid_signature",
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
    "model_not_configured",
    "model_call_outcome_unknown",
    "answer_validation_failed",
    "state_limit",
    "cancelled",
]


class AuditEntry(BaseModel):
    """Only fixed taxonomy and typed identifiers may enter the audit sink."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: UUID | None = None
    action: AuditAction
    resource_type: Literal[
        "user",
        "session",
        "repository",
        "installation",
        "webhook",
        "job",
        "credential",
        "workspace",
        "chat_run",
    ]
    resource_id: UUID | None = None
    outcome: Literal["success", "failure"]
    request_id: UUID | None = None
    job_id: UUID | None = None
    reason_code: ReasonCode | None = None

    @model_validator(mode="after")
    def failure_has_reason(self):
        if self.outcome == "failure" and self.reason_code is None:
            raise ValueError("Failure audit requires a fixed reason code")
        return self


async def write_audit(session: AsyncSession, entry: AuditEntry) -> UUID:
    values = entry.model_dump()
    values["request_id"] = entry.request_id or request_id_context.get()
    row = AuditLog(**values)
    session.add(row)
    await session.flush()
    return row.id
