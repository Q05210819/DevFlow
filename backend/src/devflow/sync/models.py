from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from devflow.db import Base
from devflow.sync.schemas import SyncCheckpoint, SyncDemandInput, SyncTarget, SyncWindow


class RepositorySync(Base):
    __tablename__ = "repository_syncs"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="generation"),
        CheckConstraint(
            "requested_sequence >= processed_sequence AND processed_sequence >= 0",
            name="sequence_order",
        ),
        CheckConstraint(
            "health_generation >= published_health_generation AND published_health_generation >= 0",
            name="health_generation_order",
        ),
        CheckConstraint("request_count >= 0", name="request_count"),
        CheckConstraint(
            "status IN ('idle','queued','running','retry_wait','completed','failed','budget_exhausted')",
            name="valid_status",
        ),
        Index("ix_repository_syncs_next_due", "next_due_at", "repository_id"),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    requested_sequence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    processed_sequence: Mapped[int] = mapped_column(BigInteger, server_default="0")
    active_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(32), server_default="idle")
    round_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    published_health_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="0"
    )
    current_health_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("health_snapshots.id")
    )


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        CheckConstraint("stream IN ('issues','pulls','actions')", name="valid_stream"),
        CheckConstraint(
            "status IN ('pending','running','complete','partial','failed','permission_missing')",
            name="valid_status",
        ),
        CheckConstraint(
            "covered_from IS NULL OR covered_until IS NULL OR covered_from <= covered_until",
            name="ordered_coverage",
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    stream: Mapped[str] = mapped_column(String(16), primary_key=True)
    checkpoint: Mapped[dict] = mapped_column(JSONB)
    covered_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    covered_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_watermark: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    status: Mapped[str] = mapped_column(String(32), server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("checkpoint")
    def typed_checkpoint(self, key, value):
        checkpoint = SyncCheckpoint.model_validate(value)
        if checkpoint.stream != self.stream:
            raise ValueError("Checkpoint stream must match its cursor")
        return checkpoint.model_dump(mode="json")


class SyncDemand(Base):
    """Demand journal; capturing a round never discards newer invalidations."""

    __tablename__ = "sync_demands"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="positive_sequence"),
        CheckConstraint(
            "reason IN ('registration','refresh','webhook','periodic','reconcile','backfill')",
            name="valid_reason",
        ),
        CheckConstraint(
            "(reason='backfill') = (backfill IS NOT NULL)", name="backfill_reason"
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reason: Mapped[str] = mapped_column(String(32))
    target: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    backfill: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))

    @validates("target")
    def typed_target(self, key, value):
        return (
            None
            if value is None
            else SyncTarget.model_validate(value).model_dump(mode="json")
        )

    @validates("backfill")
    def typed_backfill(self, key, value):
        return (
            None
            if value is None
            else SyncWindow.model_validate(value).model_dump(mode="json")
        )

    @classmethod
    def from_input(cls, value: SyncDemandInput) -> "SyncDemand":
        return cls(**SyncDemandInput.model_validate(value).model_dump())
