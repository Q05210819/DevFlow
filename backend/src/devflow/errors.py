"""Public error contract shared by API handlers and later services."""

from datetime import datetime

ERROR_CODES = frozenset(
    {
        "unauthenticated",
        "forbidden",
        "resource_not_found",
        "fact_not_found",
        "validation_error",
        "idempotency_conflict",
        "github_rate_limited",
        "github_unavailable",
        "github_configuration_error",
        "github_response_too_large",
        "github_installation_unavailable",
        "permission_missing",
        "budget_exhausted",
        "reauth_required",
        "attempt_timeout",
        "invalid_cursor",
        "service_unavailable",
        "last_admin",
        "internal_error",
        "workspace_not_found",
        "fork_source_unavailable",
        "workspace_busy",
        "version_unavailable",
        "version_changed",
        "git_failed",
        "cleanup_required",
        "storage_exhausted",
        "stage_expired",
        "payload_too_large",
        "build_deadline_exceeded",
        "index_failed",
        "index_not_ready",
        "retrieval_unavailable",
        "answer_validation_failed",
        "scope_mismatch",
        "run_in_progress",
        "conversation_archived",
        "model_not_configured",
        "model_call_outcome_unknown",
    }
)
RETRYABLE_ERROR_CODES = frozenset(
    {
        "github_rate_limited",
        "github_unavailable",
        "attempt_timeout",
        "workspace_busy",
        # Milvus/model unavailability is transient and leaves a resumable
        # version-scoped build; it must not burn the attempt budget as terminal.
        "service_unavailable",
    }
)


class AppError(Exception):
    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retry_at = retry_at
