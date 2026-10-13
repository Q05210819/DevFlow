from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from devflow.db import Base
from devflow.facts.schemas import CollectionInput, FactInput, validate_fact_payload


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "kind",
            "source_id",
            "version_key",
            name="uq_facts_source_version",
        ),
        CheckConstraint(
            "kind IN ('issue','pull','comment','review','review_comment','pr_file','ci_run','ci_job')",
            name="valid_kind",
        ),
        CheckConstraint(
            "source_id > 0 AND (number IS NULL OR number > 0) AND (parent_source_id IS NULL OR parent_source_id > 0)",
            name="positive_source_ids",
        ),
        CheckConstraint(
            "(parent_kind IS NULL) = (parent_source_id IS NULL)", name="paired_parent"
        ),
        CheckConstraint(
            "COALESCE((kind IN ('issue','pull','ci_run') AND parent_kind IS NULL) OR (kind='comment' AND parent_kind IN ('issue','pull')) OR (kind IN ('review','review_comment','pr_file') AND parent_kind='pull') OR (kind='ci_job' AND parent_kind='ci_run'), false)",
            name="valid_parent",
        ),
        Index(
            "ix_facts_repository_kind_updated_id",
            "repository_id",
            "kind",
            "source_updated_at",
            "id",
        ),
        Index(
            "ix_facts_parent",
            "repository_id",
            "parent_kind",
            "parent_source_id",
            "kind",
            "version_key",
        ),
        Index("ix_facts_repository_kind_number", "repository_id", "kind", "number"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    kind: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[int] = mapped_column(BigInteger)
    version_key: Mapped[str] = mapped_column(String(256), server_default="")
    number: Mapped[int | None] = mapped_column(BigInteger)
    parent_kind: Mapped[str | None] = mapped_column(String(16))
    parent_source_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str | None] = mapped_column(String(32))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    source_url: Mapped[str] = mapped_column(Text)
    unavailable: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    payload: Mapped[dict] = mapped_column(JSONB)

    @validates("payload")
    def whitelist_payload(self, key, value):
        return validate_fact_payload(self.kind, value).model_dump(mode="json")

    @classmethod
    def from_input(cls, value: FactInput) -> "Fact":
        value = FactInput.model_validate(value)
        return cls(**value.model_dump())


class FactCollection(Base):
    __tablename__ = "fact_collections"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "parent_kind",
            "parent_source_id",
            "collection",
            "version_key",
            name="uq_fact_collections_parent_version",
        ),
        CheckConstraint("parent_source_id > 0", name="positive_parent_id"),
        CheckConstraint(
            "(parent_kind='issue' AND collection='comments') OR (parent_kind='pull' AND collection IN ('comments','reviews','review_comments','files')) OR (parent_kind='ci_run' AND collection IN ('attempts','jobs'))",
            name="valid_collection",
        ),
        CheckConstraint(
            "(complete AND reason IS NULL) OR (NOT complete AND reason IS NOT NULL)",
            name="complete_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"))
    parent_kind: Mapped[str] = mapped_column(String(16))
    parent_source_id: Mapped[int] = mapped_column(BigInteger)
    collection: Mapped[str] = mapped_column(String(32))
    version_key: Mapped[str] = mapped_column(String(256), server_default="")
    complete: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    @classmethod
    def from_input(cls, value: CollectionInput) -> "FactCollection":
        return cls(**CollectionInput.model_validate(value).model_dump())
