from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base

WEBHOOK_EVENTS = (
    "installation",
    "installation_repositories",
    "github_app_authorization",
    "unknown",
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "workflow_run",
    "workflow_job",
    "push",
    "create",
    "delete",
    "repository",
)
WEBHOOK_ACTIONS = (
    "created",
    "deleted",
    "suspend",
    "unsuspend",
    "new_permissions_accepted",
    "added",
    "removed",
    "revoked",
    "opened",
    "edited",
    "closed",
    "reopened",
    "assigned",
    "unassigned",
    "labeled",
    "unlabeled",
    "locked",
    "unlocked",
    "milestoned",
    "demilestoned",
    "pinned",
    "unpinned",
    "transferred",
    "typed",
    "untyped",
    "synchronize",
    "converted_to_draft",
    "ready_for_review",
    "review_requested",
    "review_request_removed",
    "enqueued",
    "dequeued",
    "auto_merge_enabled",
    "auto_merge_disabled",
    "submitted",
    "dismissed",
    "requested",
    "in_progress",
    "completed",
    "queued",
    "waiting",
    "pending",
    "renamed",
    "privatized",
    "publicized",
    "archived",
    "unarchived",
)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('processed', 'ignored')", name="valid_status"),
        CheckConstraint(
            f"event_name IN {WEBHOOK_EVENTS}",
            name="valid_event",
        ),
        CheckConstraint(
            f"action IS NULL OR action IN {WEBHOOK_ACTIONS}",
            name="valid_action",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(unique=True)
    event_name: Mapped[str] = mapped_column(String(32))
    action: Mapped[str | None] = mapped_column(String(32))
    # External identifiers are observations, including unknown local actors.
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    github_repository_id: Mapped[int | None] = mapped_column(BigInteger)
    target_kind: Mapped[str | None] = mapped_column(String(16))
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    number: Mapped[int | None] = mapped_column(BigInteger)
    attempt: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class GitHubInstallation(Base):
    __tablename__ = "github_installations"
    __table_args__ = (
        CheckConstraint("installation_id > 0", name="positive_installation_id"),
        CheckConstraint("auth_version >= 1", name="positive_auth_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    app_id: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    available: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    needs_verification: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )
    auth_version: Mapped[int] = mapped_column(Integer, server_default="1")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GitHubCredential(Base):
    __tablename__ = "github_credentials"
    __table_args__ = (CheckConstraint("version >= 1", name="positive_version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    needs_reauth: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
