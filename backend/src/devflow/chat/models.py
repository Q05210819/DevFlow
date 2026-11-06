"""Durable chat metadata: private conversations, runs, tools and bounded budgets.

Everything here is PostgreSQL state. LangGraph checkpoints and model payloads are
bounded by later tasks; a row in these tables never grants access by itself and
is always re-authorized against every participating repository epoch.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from devflow.db import Base


class Conversation(Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "idempotency_key", name="uq_chat_conversations_actor_key"
        ),
        CheckConstraint(
            "idempotency_key ~ '^[!-~]{1,128}$'", name="ck_chat_conversations_key"
        ),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'", name="ck_chat_conversations_input_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )


class ConversationRepository(Base):
    """The immutable, ordered source set chosen when the conversation was made."""

    __tablename__ = "chat_conversation_repositories"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "ordinal", name="uq_chat_conversation_repository_ordinal"
        ),
        CheckConstraint(
            "ordinal BETWEEN 0 AND 2", name="ck_chat_conversation_repositories_ordinal"
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class AgentRun(Base):
    """One durable question/answer attempt; the Job is its execution lease.

    ``selection`` is the canonical user scope frozen at submit time.
    ``scope`` stores a later frozen retrieval scope so a restarted run can prove
    it never widened what the user selected. Counters and deadlines survive a
    worker restart; model and pricing state live in the budget tables.
    """

    __tablename__ = "chat_agent_runs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_chat_agent_runs_job"),
        UniqueConstraint("thread_id", name="uq_chat_agent_runs_thread"),
        Index(
            "uq_chat_agent_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("terminal_at IS NULL"),
        ),
        Index(
            "ix_chat_agent_runs_conversation_created", "conversation_id", "created_at"
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed')",
            name="ck_chat_agent_runs_status",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed')) = (terminal_at IS NOT NULL)",
            name="ck_chat_agent_runs_terminal",
        ),
        CheckConstraint(
            "(started_at IS NULL) = (deadline IS NULL)",
            name="ck_chat_agent_runs_deadline",
        ),
        CheckConstraint(
            "nodes_used BETWEEN 0 AND 8 AND retrievals_used BETWEEN 0 AND 2 "
            "AND model_requests_used BETWEEN 0 AND 2",
            name="ck_chat_agent_runs_budget",
        ),
        CheckConstraint(
            "octet_length(selection::text) <= 16384",
            name="ck_chat_agent_runs_selection",
        ),
        CheckConstraint(
            "scope IS NULL OR octet_length(scope::text) <= 65536",
            name="ck_chat_agent_runs_scope",
        ),
        CheckConstraint(
            "accepted_config IS NULL OR octet_length(accepted_config::text) <= 65536",
            name="ck_chat_agent_runs_config",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("chat_conversations.id"))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    thread_id: Mapped[UUID] = mapped_column(default=uuid4)
    graph_version: Mapped[str] = mapped_column(String(64))
    selection: Mapped[dict] = mapped_column(JSONB)
    scope: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    nodes_used: Mapped[int] = mapped_column(Integer, server_default="0")
    retrievals_used: Mapped[int] = mapped_column(Integer, server_default="0")
    model_requests_used: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    queued_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_config: Mapped[dict | None] = mapped_column(JSONB)
    accepted_retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    terminal_reason: Mapped[str | None] = mapped_column(String(64))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )


class Message(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "ordinal", name="uq_chat_messages_conversation_ordinal"
        ),
        Index(
            "uq_chat_messages_run_role",
            "run_id",
            "role",
            unique=True,
            postgresql_where=text("run_id IS NOT NULL"),
        ),
        CheckConstraint("ordinal >= 0", name="ck_chat_messages_ordinal"),
        CheckConstraint("role IN ('user','assistant')", name="ck_chat_messages_role"),
        CheckConstraint(
            "body_bytes = octet_length(body) AND "
            "((role='user' AND body_bytes BETWEEN 1 AND 2048) OR "
            "(role='assistant' AND body_bytes BETWEEN 0 AND 16384))",
            name="ck_chat_messages_body",
        ),
        CheckConstraint(
            "manifest IS NULL OR octet_length(manifest::text) <= 65536",
            name="ck_chat_messages_manifest",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("chat_agent_runs.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    body_bytes: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class ToolCall(Base):
    """Bounded per-run tool record; four calls at most, never a second queue."""

    __tablename__ = "chat_tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_chat_tool_calls_run_ordinal"),
        CheckConstraint("ordinal BETWEEN 0 AND 3", name="ck_chat_tool_calls_ordinal"),
        CheckConstraint(
            "status IN ('requested','succeeded','failed')",
            name="ck_chat_tool_calls_status",
        ),
        CheckConstraint(
            "octet_length(arguments::text) <= 16384",
            name="ck_chat_tool_calls_arguments",
        ),
        CheckConstraint(
            "result IS NULL OR octet_length(result::text) <= 131072",
            name="ck_chat_tool_calls_result",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed')) = (completed_at IS NOT NULL)",
            name="ck_chat_tool_calls_completed",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    arguments: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), server_default="requested")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BudgetLedger(Base):
    """Run-scoped reservation plus the actor's monthly settled ledger.

    No price or currency is invented here: Task 2 fills these rows only from
    accepted provider evidence.
    """

    __tablename__ = "chat_budget_ledgers"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_chat_budget_ledgers_run"),
        UniqueConstraint(
            "actor_id", "month_start", "currency", name="uq_chat_budget_ledgers_month"
        ),
        CheckConstraint(
            "ledger_scope IN ('run','month')", name="ck_chat_budget_ledgers_scope"
        ),
        CheckConstraint(
            "(ledger_scope='run' AND run_id IS NOT NULL AND month_start IS NULL) OR "
            "(ledger_scope='month' AND run_id IS NULL AND month_start IS NOT NULL)",
            name="ck_chat_budget_ledgers_shape",
        ),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_chat_budget_ledgers_currency"
        ),
        CheckConstraint(
            "reserved >= 0 AND settled >= 0", name="ck_chat_budget_ledgers_amounts"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ledger_scope: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("chat_agent_runs.id"))
    month_start: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    reserved: Mapped[Decimal] = mapped_column(Numeric(24, 6), server_default="0")
    settled: Mapped[Decimal] = mapped_column(Numeric(24, 6), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        onupdate=func.clock_timestamp(),
    )


class RequestReceipt(Base):
    """One logical model request; a replay never becomes a second dispatch."""

    __tablename__ = "chat_request_receipts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "kind", "ordinal", name="uq_chat_request_receipts_logical"
        ),
        CheckConstraint("kind IN ('model')", name="ck_chat_request_receipts_kind"),
        CheckConstraint("ordinal >= 0", name="ck_chat_request_receipts_ordinal"),
        CheckConstraint(
            "status IN ('reserved','dispatched','settled','unknown')",
            name="ck_chat_request_receipts_status",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_chat_request_receipts_hash",
        ),
        CheckConstraint(
            "input_cap > 0 AND output_cap > 0",
            name="ck_chat_request_receipts_caps",
        ),
        CheckConstraint(
            "(amount IS NULL) = (currency IS NULL) AND (amount IS NULL OR amount >= 0)",
            name="ck_chat_request_receipts_amount",
        ),
        CheckConstraint(
            "usage IS NULL OR octet_length(usage::text) <= 4096",
            name="ck_chat_request_receipts_usage",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_agent_runs.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), server_default="model")
    ordinal: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    parameter_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="reserved")
    input_cap: Mapped[int] = mapped_column(BigInteger)
    output_cap: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    pricing_version: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
