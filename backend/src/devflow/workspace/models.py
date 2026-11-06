"""Provenance stays distinct from shared Git objects and physical process guards."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class WorkspaceVersion(Base):
    __tablename__ = "workspace_versions"
    __mapper_args__: ClassVar = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint(
            "requested_by",
            "repository_id",
            "idempotency_key",
            name="uq_workspace_request",
        ),
        CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{40}$' AND tree_oid ~ '^[0-9a-f]{40}$'",
            name="object_ids",
        ),
        CheckConstraint(
            "status IN ('queued','ready','failed','unavailable')", name="status"
        ),
        CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$' AND input_hash ~ '^[0-9a-f]{64}$'",
            name="request_identity",
        ),
        Index("ix_workspace_repository_created", "repository_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    source_repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    cache_repository_id: Mapped[int] = mapped_column(BigInteger)
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    selection: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[dict] = mapped_column(JSONB)
    scopes: Mapped[list] = mapped_column(JSONB)
    commit_sha: Mapped[str] = mapped_column(String(40))
    tree_oid: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    pin_ref: Mapped[str | None] = mapped_column(String(128))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class WorkspaceProcessGuard(Base):
    __tablename__ = "workspace_process_guards"
    __table_args__ = (
        CheckConstraint(
            "operation_id ~ '^[0-9a-f]{32}$' AND nonce ~ '^[0-9a-f]{32}$'",
            name="identity",
        ),
        CheckConstraint("kind IN ('read','write','gc')", name="kind"),
        CheckConstraint(
            "state IN ('reserved','active','cleanup_required','drained')", name="state"
        ),
        CheckConstraint("repository_id > 0", name="repository"),
        Index("ix_workspace_guards_admission", "state", "kind", "repository_id"),
    )
    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32))
    repository_id: Mapped[int] = mapped_column(BigInteger)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspace_versions.id"))
    kind: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(24), server_default="reserved")
    runtime_id: Mapped[UUID]
    lock_path: Mapped[str] = mapped_column(String(4096))
    deadline: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list] = mapped_column(JSONB)
    actor: Mapped[dict | None] = mapped_column(JSONB)
    lease: Mapped[dict | None] = mapped_column(JSONB)
    identity: Mapped[dict | None] = mapped_column(JSONB)
    proof: Mapped[str | None] = mapped_column(String(32))
    drained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
