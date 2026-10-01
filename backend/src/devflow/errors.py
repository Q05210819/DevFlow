"""Public error contract shared by API handlers and later services."""

from datetime import datetime

ERROR_CODES = frozenset(
    {
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
        "invalid_cursor",
        "service_unavailable",
        "last_admin",
        "internal_error",
    }
)
RETRYABLE_ERROR_CODES = frozenset(
    {"github_rate_limited", "github_unavailable", "attempt_timeout"}
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
