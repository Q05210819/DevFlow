from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        CheckConstraint("github_repository_id > 0", name="positive_github_id"),
        CheckConstraint("auth_version >= 1", name="positive_auth_version"),
        Index("ix_repositories_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    github_repository_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    installation_id: Mapped[int] = mapped_column(
        ForeignKey("github_installations.installation_id"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(512))
    private: Mapped[bool] = mapped_column(Boolean)
    default_branch: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
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
