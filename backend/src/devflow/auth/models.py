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
    Sequence,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base

AUTH_ORDER_SEQUENCE = Sequence(
    "auth_change_order_seq", metadata=Base.metadata, start=1, increment=1, cache=1
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="valid_role"),
        CheckConstraint("github_id > 0", name="positive_github_id"),
        CheckConstraint("auth_version >= 1", name="positive_auth_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), server_default="member")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    auth_version: Mapped[int] = mapped_column(Integer, server_default="1")
    # Time remains useful for audit; the shared sequence establishes ordering
    # even when the database wall clock moves backward.
    auth_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    auth_changed_order: Mapped[int] = mapped_column(
        BigInteger, server_default=AUTH_ORDER_SEQUENCE.next_value()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_nonce_hash: Mapped[str] = mapped_column(String(64))
    pkce_verifier_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    return_path: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_order: Mapped[int] = mapped_column(
        BigInteger, server_default=AUTH_ORDER_SEQUENCE.next_value()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
