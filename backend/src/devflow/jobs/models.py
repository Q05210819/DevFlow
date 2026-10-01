from datetime import datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class Job(Base):
    __tablename__ = "jobs"
    __mapper_args__: ClassVar = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint(
            "requested_by", "kind", "idempotency_key", name="uq_jobs_creator_kind_key"
        ),
        CheckConstraint("kind = 'repository.access_probe'", name="valid_kind"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
            name="valid_status",
        ),
        CheckConstraint(
            "attempt >= 0 AND attempt <= max_attempts AND max_attempts BETWEEN 1 AND 3",
            name="attempt_budget",
        ),
        CheckConstraint("lease_generation >= 0", name="lease_generation"),
        CheckConstraint("last_sequence >= 0", name="last_sequence"),
        CheckConstraint("idempotency_key ~ '^[!-~]{1,128}$'", name="idempotency_key"),
        CheckConstraint("input_hash ~ '^[0-9a-f]{64}$'", name="input_hash"),
        Index("ix_jobs_due", "status", "available_at", "created_at", "id"),
        Index("ix_jobs_expired_lease", "status", "lease_expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(
        String(64), server_default="repository.access_probe"
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), index=True
    )
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    input: Mapped[dict] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    attempt: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    lease_owner: Mapped[UUID | None]
    lease_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_sequence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_run_events_job_sequence"),
        CheckConstraint("sequence >= 1", name="positive_sequence"),
        CheckConstraint("event = 'job.status'", name="valid_event"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    event: Mapped[str] = mapped_column(String(32), server_default="job.status")
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
