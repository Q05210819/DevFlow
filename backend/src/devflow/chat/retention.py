"""Independent, fenced deletion of terminal official checkpoint threads.

Only rows whose run is terminal, has retired its accepted config and no longer
has one may be deleted from the official schema. The same sweep also reclaims
content whose retention has expired: terminal-run tool calls past their fixed
expiry, message bodies older than the history window and settled receipts of
terminal runs. Active runs, unknown reservations, monthly aggregates and
unexpired messages are never touched. The sweep is idempotent: an interrupted
deletion is simply retried by the next ``chat.cleanup`` Job.

``ChatMaintenance`` is the deterministic producer of those Jobs. It runs inside
the existing worker maintenance tick, so chat retention needs no new daemon and
never occupies a model slot: ``chat.cleanup`` is an ordinary Job kind.
"""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import ValidationError
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from devflow.chat.budget import prune_settled_receipts
from devflow.chat.models import (
    AgentRun,
    CheckpointAccount,
    Message,
    RequestReceipt,
    ToolCall,
)
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import ChatCleanupInput, ChatCleanupResult
from devflow.jobs.service import append_event
from devflow.repositories.models import Repository
from devflow.sync.scope import transaction

CLEANUP_BATCH = 100
_ADVISORY_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('devflow:chat:checkpoints', 0))"
)
_CANDIDATES_SQL = (
    "SELECT a.run_id, a.thread_id, a.bytes "
    "FROM chat_checkpoint_accounts a "
    "JOIN chat_agent_runs r ON r.id = a.run_id "
    "WHERE r.terminal_at IS NOT NULL "
    "AND r.accepted_retired_at IS NOT NULL "
    "AND r.accepted_config IS NULL "
    "ORDER BY r.terminal_at, r.id "
    "LIMIT %s"
)
_RETENTION_SQL = text("interval '90 days'")


def _invalid() -> AppError:
    return AppError("validation_error", 422, "Invalid cleanup request")


def _empty() -> ChatCleanupResult:
    return ChatCleanupResult(examined=0, reclaimed=0, bytes_reclaimed=0)


def _raw_dsn(factory) -> str:
    return (
        factory.kw["bind"]
        .url.set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


class ChatRetention:
    """Terminal-thread checkpoint retention keyed by its own Job lease."""

    def __init__(self, factory):
        self.factory = factory

    async def sweep(self, lease, *, limit: int = CLEANUP_BATCH) -> ChatCleanupResult:
        if type(limit) is not int or not 1 <= limit <= CLEANUP_BATCH:
            raise _invalid()
        async with transaction(self.factory) as db:
            job, _ = await Queue(self.factory)._owned(db, lease)
            if job is None:
                return _empty()
            if job.kind != "chat.cleanup":
                raise _invalid()
            try:
                payload = ChatCleanupInput.model_validate(job.input)
            except ValidationError:
                raise _invalid() from None
            lease_expiry = job.lease_expires_at
            batch = min(limit, payload.limit)

            candidates = list(
                await db.execute(
                    select(
                        CheckpointAccount.run_id,
                        CheckpointAccount.thread_id,
                    )
                    .join(AgentRun, AgentRun.id == CheckpointAccount.run_id)
                    .where(
                        AgentRun.terminal_at.is_not(None),
                        AgentRun.accepted_retired_at.is_not(None),
                        AgentRun.accepted_config.is_(None),
                    )
                    .order_by(AgentRun.terminal_at, AgentRun.id)
                    .limit(batch)
                )
            )

            (
                tool_calls_reclaimed,
                messages_reclaimed,
                receipts_reclaimed,
            ) = await self._reclaim_content(db, batch)

            examined, reclaimed, bytes_reclaimed = await self._delete(
                [thread_id for _, thread_id in candidates], batch
            )

            finished_at = await db.scalar(select(func.clock_timestamp()))
            job.status, job.error_code = "succeeded", None
            result = ChatCleanupResult(
                examined=examined,
                reclaimed=reclaimed,
                bytes_reclaimed=bytes_reclaimed,
                tool_calls_reclaimed=tool_calls_reclaimed,
                messages_reclaimed=messages_reclaimed,
                receipts_reclaimed=receipts_reclaimed,
            )
            job.result = result.model_dump(mode="json")
            job.lease_owner = job.lease_expires_at = None
            job.updated_at = finished_at
            await Queue._transition(db, job, "job.succeeded")
            final_now = await db.scalar(select(func.clock_timestamp()))
            if final_now >= lease_expiry:
                raise AppError("lease_expired", 409, "Cleanup lease expired")
            return result

    async def _reclaim_content(self, db, batch: int) -> tuple[int, int, int]:
        """Delete only expired content; unknown fee evidence always survives."""
        tool_ids = list(
            await db.scalars(
                select(ToolCall.id)
                .join(AgentRun, AgentRun.id == ToolCall.run_id)
                .where(
                    AgentRun.terminal_at.is_not(None),
                    ToolCall.expires_at <= func.clock_timestamp(),
                )
                .order_by(ToolCall.expires_at, ToolCall.id)
                .limit(batch)
                .with_for_update(of=ToolCall, skip_locked=True)
            )
        )
        if tool_ids:
            await db.execute(sa_delete(ToolCall).where(ToolCall.id.in_(tool_ids)))
        message_ids = list(
            await db.scalars(
                select(Message.id)
                .where(Message.created_at <= func.clock_timestamp() - _RETENTION_SQL)
                .order_by(Message.created_at, Message.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        )
        if message_ids:
            await db.execute(sa_delete(Message).where(Message.id.in_(message_ids)))
        receipts = await prune_settled_receipts(db, limit=batch)
        return len(tool_ids), len(message_ids), receipts

    async def _delete(self, threads, batch: int) -> tuple[int, int, int]:
        """Delete official rows and accounts under the shared advisory lock.

        The candidate predicate is re-checked after the lock is held, so a run
        that became active again (or a stale account) is never deleted from a
        decision made before the lock.
        """

        if not threads:
            return 0, 0, 0
        raw = await psycopg.AsyncConnection.connect(_raw_dsn(self.factory))
        async with raw, raw.transaction(), raw.cursor() as cursor:
            await cursor.execute(_ADVISORY_SQL)
            await cursor.execute("SET LOCAL statement_timeout = '4s'")
            await cursor.execute("SET LOCAL lock_timeout = '2s'")
            await cursor.execute(_CANDIDATES_SQL + " FOR UPDATE OF a", (batch,))
            rows = await cursor.fetchall()
            saver = AsyncPostgresSaver(raw)
            examined = len(rows)
            reclaimed = 0
            total_bytes = 0
            for run_id, thread_id, size in rows:
                await saver.adelete_thread(str(thread_id))
                await cursor.execute(
                    "DELETE FROM chat_checkpoint_accounts WHERE run_id = %s",
                    (str(run_id),),
                )
                reclaimed += 1
                total_bytes += size
            return examined, reclaimed, total_bytes


class ChatMaintenance:
    """Deterministic producer of bounded ``chat.cleanup`` Jobs.

    Called from the existing worker maintenance tick. At most one Job is
    inserted per UTC hour bucket, keyed by the anchor repository; a repeated
    tick, a second worker or a restarted process collides with the partial
    unique index and produces nothing. The Job is ordinary work, so it never
    takes the model worker's serialized inference slot.
    """

    def __init__(self, factory):
        self.factory = factory

    async def tick(self) -> bool:
        async with transaction(self.factory) as db:
            if not await self._due(db):
                return False
            anchor = await db.scalar(
                select(Repository.id).order_by(Repository.id).limit(1)
            )
            if anchor is None:
                return False
            bucket = datetime.now(UTC).strftime("%Y%m%d%H")
            key = f"chat-maintenance:{bucket}"
            job_id = await db.scalar(
                insert(Job)
                .values(
                    id=uuid4(),
                    kind="chat.cleanup",
                    repository_id=anchor,
                    requested_by=None,
                    input={"version": 1, "limit": CLEANUP_BATCH},
                    input_hash=hashlib.sha256(key.encode()).hexdigest(),
                    idempotency_key=key,
                    max_attempts=3,
                )
                .on_conflict_do_nothing(
                    index_elements=["repository_id", "kind", "idempotency_key"],
                    index_where=text("requested_by IS NULL"),
                )
                .returning(Job.id)
            )
            if job_id is None:
                return False
            job = await db.get(Job, job_id)
            if job is None:
                return False
            await append_event(db, job, "queued")
            return True

    @staticmethod
    async def _due(db) -> bool:
        checkpoint = await db.scalar(
            select(CheckpointAccount.run_id)
            .join(AgentRun, AgentRun.id == CheckpointAccount.run_id)
            .where(
                AgentRun.terminal_at.is_not(None),
                AgentRun.accepted_retired_at.is_not(None),
                AgentRun.accepted_config.is_(None),
            )
            .limit(1)
        )
        if checkpoint is not None:
            return True
        tool = await db.scalar(
            select(ToolCall.id)
            .join(AgentRun, AgentRun.id == ToolCall.run_id)
            .where(
                AgentRun.terminal_at.is_not(None),
                ToolCall.expires_at <= func.clock_timestamp(),
            )
            .limit(1)
        )
        if tool is not None:
            return True
        message = await db.scalar(
            select(Message.id)
            .where(Message.created_at <= func.clock_timestamp() - _RETENTION_SQL)
            .limit(1)
        )
        if message is not None:
            return True
        receipt = await db.scalar(
            select(RequestReceipt.id)
            .join(AgentRun, AgentRun.id == RequestReceipt.run_id)
            .where(
                RequestReceipt.status == "settled",
                AgentRun.terminal_at.is_not(None),
                RequestReceipt.settled_at <= func.clock_timestamp() - _RETENTION_SQL,
            )
            .limit(1)
        )
        return receipt is not None
