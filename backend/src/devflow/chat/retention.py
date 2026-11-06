"""Independent, fenced deletion of terminal official checkpoint threads.

Only rows whose run is terminal, has retired its accepted config and no longer
has one may be deleted. Messages, manifests, receipts and budget ledgers live
outside the official schema and are never touched here. The sweep is idempotent:
an interrupted deletion is simply retried by the next ``chat.cleanup`` Job.
"""

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import ValidationError
from sqlalchemy import func, select

from devflow.chat.models import AgentRun, CheckpointAccount
from devflow.errors import AppError
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import ChatCleanupInput, ChatCleanupResult
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

            examined, reclaimed, bytes_reclaimed = await self._delete(
                [thread_id for _, thread_id in candidates], batch
            )

            finished_at = await db.scalar(select(func.clock_timestamp()))
            job.status, job.error_code = "succeeded", None
            job.result = ChatCleanupResult(
                examined=examined,
                reclaimed=reclaimed,
                bytes_reclaimed=bytes_reclaimed,
            ).model_dump(mode="json")
            job.lease_owner = job.lease_expires_at = None
            job.updated_at = finished_at
            await Queue._transition(db, job, "job.succeeded")
            final_now = await db.scalar(select(func.clock_timestamp()))
            if final_now >= lease_expiry:
                raise AppError("lease_expired", 409, "Cleanup lease expired")
            return ChatCleanupResult(
                examined=examined,
                reclaimed=reclaimed,
                bytes_reclaimed=bytes_reclaimed,
            )

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
