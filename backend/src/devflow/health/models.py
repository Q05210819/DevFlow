from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class HealthSnapshot(Base):
    __tablename__ = "health_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "generation",
            name="uq_health_snapshots_repository_generation",
        ),
        CheckConstraint("generation >= 1", name="positive_generation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    generation: Mapped[int] = mapped_column(BigInteger)
    rule_version: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    manifest: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
