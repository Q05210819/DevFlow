"""Fenced access to the official LangGraph PostgreSQL saver.

The official ``AsyncPostgresSaver`` owns checkpoint encoding and schema. This
module adds the only authority LangGraph cannot know about: every read or write
must hold the Job lease, the exact source grant and the non-terminal run while
the accepted config, the official rows and the per-run accounting commit in one
transaction. A stale writer can therefore never publish state or observe state
it is no longer entitled to.

No attempt-scoped ``checkpoint_ns`` is allowed: exactly one linear, accepted
thread per run. The accepted config row is the only readable pointer; official
rows without it are unapproved garbage and are reclaimed by fenced cleanup.
"""

import argparse
import asyncio
import json
import weakref
from typing import Any
from uuid import UUID

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb

from devflow.config import Settings
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.jobs.queue import Lease
from devflow.knowledge.schemas import SourceGrant

MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CHECKPOINTS_PER_RUN = 32
MAX_GLOBAL_BYTES = 128 * 1024 * 1024

_ADVISORY_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('devflow:chat:checkpoints', 0))"
)

# psycopg connections are not re-entrant; hidden savers on one connection must
# share one lock even though the official saver only locks its own instance.
_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _invalid() -> AppError:
    return AppError("validation_error", 422, "Invalid checkpoint operation")


def _lease_expired() -> AppError:
    return AppError("lease_expired", 409, "Run lease expired")


def _state_limit() -> AppError:
    return AppError("state_limit", 409, "Chat checkpoint storage limit reached")


def _lock_for(connection) -> asyncio.Lock | None:
    try:
        lock = _LOCKS.get(connection)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[connection] = lock
        return lock
    except TypeError:
        # Some async connection doubles are not weak-referenceable.
        return None


def _canonical_uuid(value, *, optional: bool):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _invalid()
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise _invalid() from None
    if str(parsed) != value:
        raise _invalid()
    return parsed


async def _store_accepted(cursor, run_id: UUID, accepted: dict) -> None:
    """Publish the accepted config together with the official write.

    Kept module-level so tests can inject a failure after the official rows are
    written and prove the whole transaction rolls back.
    """

    await cursor.execute(
        "UPDATE chat_agent_runs SET accepted_config = %s, "
        "updated_at = clock_timestamp() "
        "WHERE id = %s AND terminal_at IS NULL",
        (Jsonb(accepted), str(run_id)),
    )
    if cursor.rowcount != 1:
        raise _lease_expired()


class FencedPostgresSaver(BaseCheckpointSaver):
    """One run's official saver, fenced by lease, grant and accepted pointer."""

    def __init__(
        self,
        connection,
        lease: Lease,
        run_id: UUID,
        grant: SourceGrant,
        *,
        settings: Settings | None = None,
    ) -> None:
        super().__init__()
        if not isinstance(run_id, UUID):
            raise _invalid()
        if not connection.autocommit:
            # Only an autocommit raw connection makes ``transaction()`` a real
            # unit that commits. On a non-autocommit connection it would become
            # a savepoint hidden inside someone else's open transaction.
            raise ValueError("Fenced saver requires an autocommit connection")
        self._connection = connection
        self._lease = lease
        self._run_id = run_id
        self._grant = grant
        self._settings = settings
        self._saver = AsyncPostgresSaver(connection)
        self._fallback_lock = asyncio.Lock()

    def _mutex(self) -> asyncio.Lock:
        return _lock_for(self._connection) or self._fallback_lock

    # ----------------------------------------------------------- public API

    async def aput(
        self,
        config,
        checkpoint: dict,
        metadata: dict,
        new_versions: dict,
    ) -> dict:
        thread, parent = self._parse_config(config)
        self._check_size(checkpoint, metadata)
        async with self._mutex(), self._connection.transaction():
            accepted_id = await self._fence(thread, parent, mode="aput")
            next_config = await self._saver.aput(
                self._official_config(thread, accepted_id, parent=parent),
                checkpoint,
                metadata,
                new_versions,
            )
            async with self._connection.cursor() as cursor:
                await _store_accepted(cursor, self._run_id, next_config)
                await self._account(cursor, thread)
        return next_config

    async def aput_writes(
        self, config, writes, task_id: str, task_path: str = ""
    ) -> None:
        thread, checkpoint_id = self._parse_config(config)
        if not isinstance(task_id, str) or not task_id:
            raise _invalid()
        async with self._mutex(), self._connection.transaction():
            accepted_id = await self._fence(thread, checkpoint_id, mode="writes")
            await self._saver.aput_writes(
                self._official_config(thread, accepted_id, parent=checkpoint_id),
                writes,
                task_id,
                task_path,
            )
            async with self._connection.cursor() as cursor:
                await self._account(cursor, thread)

    async def aget_tuple(self, config):
        thread, checkpoint_id = self._parse_config(config)
        async with self._mutex(), self._connection.transaction():
            accepted_id = await self._fence(thread, checkpoint_id, mode="read")
            if accepted_id is None:
                return None
            return await self._saver.aget_tuple(
                self._official_config(thread, accepted_id)
            )

    # ------------------------------------------------------------- configs

    def get_next_version(self, current, channel=None):
        """Expose the official version allocator as a plain attribute.

        Pregel reads this off the checkpointer during loop construction and
        calls it unchanged, so it must not be a coroutine.
        """
        return self._saver.get_next_version(current, channel)

    @staticmethod
    def _parse_config(config) -> tuple[str, str | None]:
        if not isinstance(config, dict):
            raise _invalid()
        configurable = config.get("configurable")
        # Pregel adds its own keys (runtime, durability, resuming, ...) to the
        # configurable block it hands the checkpointer; only our three keys are
        # authority-bearing and validated here. A top-level loop omits
        # ``checkpoint_ns`` entirely, which the official saver also reads as the
        # empty root namespace; any non-empty namespace is still rejected.
        if not isinstance(configurable, dict):
            raise _invalid()
        if configurable.get("checkpoint_ns", "") != "":
            raise _invalid()
        thread = configurable.get("thread_id")
        if not isinstance(thread, str):
            raise _invalid()
        _canonical_uuid(thread, optional=False)
        checkpoint_id = _canonical_uuid(
            configurable.get("checkpoint_id"), optional=True
        )
        return thread, str(checkpoint_id) if checkpoint_id is not None else None

    @staticmethod
    def _official_config(
        thread: str, accepted_id: str | None, *, parent: str | None = None
    ) -> dict:
        configurable = {"thread_id": thread, "checkpoint_ns": ""}
        selected = accepted_id if accepted_id is not None else parent
        if selected is not None:
            configurable["checkpoint_id"] = selected
        return {"configurable": configurable}

    # ---------------------------------------------------------------- size

    def _check_size(self, checkpoint, metadata) -> None:
        if not isinstance(checkpoint, dict):
            raise _invalid()
        values = checkpoint.get("channel_values")
        if not isinstance(values, dict):
            raise _invalid()
        total = 256
        for key, value in values.items():
            if not isinstance(key, str):
                raise _invalid()
            total += len(key.encode("utf-8")) + 16
            if value is None:
                total += 8
            elif isinstance(value, str):
                total += len(value.encode("utf-8"))
            elif isinstance(value, (bool, int, float)):
                total += 8
            else:
                _, payload = self._saver.serde.dumps_typed(value)
                total += len(payload)
        if metadata:
            total += len(
                json.dumps(
                    metadata, default=str, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        if total > MAX_CHECKPOINT_BYTES:
            raise _state_limit()

    # ---------------------------------------------------------------- fence

    async def _fence(
        self, thread: str, supplied: str | None, *, mode: str
    ) -> str | None:
        grant = self._grant
        if not isinstance(grant, SourceGrant) or not grant.repository_epochs:
            raise not_found()
        actor = grant.actor
        thread_id = UUID(thread)
        lease = self._lease

        async with self._connection.cursor() as cursor:
            await cursor.execute(_ADVISORY_SQL)

            await cursor.execute(
                "SELECT enabled, auth_version, github_id, role "
                "FROM users WHERE id = %s FOR UPDATE",
                (str(actor.id),),
            )
            user = await cursor.fetchone()
            if user is None:
                raise not_found()
            enabled, user_version, github_id, role = user
            if not enabled or (user_version, github_id, role) != (
                actor.auth_version,
                actor.github_id,
                actor.role,
            ):
                raise not_found()

            installations = {}
            for epoch in grant.repository_epochs:
                previous = installations.setdefault(epoch.installation_id, epoch)
                if previous.installation_version != epoch.installation_version:
                    raise not_found()
            identities = sorted(installations)
            await cursor.execute(
                "SELECT installation_id, available, needs_verification, auth_version "
                "FROM github_installations WHERE installation_id = ANY(%s) "
                "ORDER BY installation_id FOR UPDATE",
                (identities,),
            )
            rows = await cursor.fetchall()
            if len(rows) != len(identities):
                raise not_found()
            for identity, available, needs_verification, version in rows:
                if (
                    not available
                    or needs_verification
                    or version != installations[identity].installation_version
                ):
                    raise not_found()
                if self._settings is not None and (
                    identity not in self._settings.github_allowed_installation_ids
                ):
                    raise not_found()

            epochs = {}
            for epoch in grant.repository_epochs:
                if epoch.repository_id in epochs:
                    raise not_found()
                epochs[epoch.repository_id] = epoch
            ordered = sorted(epochs, key=str)
            await cursor.execute(
                "SELECT id, installation_id, enabled, available, "
                "needs_verification, auth_version FROM repositories "
                "WHERE id = ANY(%s::uuid[]) ORDER BY id::text FOR UPDATE",
                ([str(item) for item in ordered],),
            )
            rows = await cursor.fetchall()
            if len(rows) != len(ordered):
                raise not_found()
            for identity, installation_id, enabled, available, needs, version in rows:
                epoch = epochs[identity]
                if (
                    not enabled
                    or not available
                    or needs
                    or installation_id != epoch.installation_id
                    or version != epoch.repository_version
                ):
                    raise not_found()

            await cursor.execute(
                "SELECT kind, status, lease_owner, lease_generation, attempt "
                "FROM jobs WHERE id = %s FOR UPDATE",
                (str(lease.job_id),),
            )
            job = await cursor.fetchone()
            if job is None:
                raise _lease_expired()
            kind, status, owner, generation, attempt = job
            if (
                kind != "chat.run"
                or status != "running"
                or owner != lease.owner
                or generation != lease.generation
                or attempt != lease.attempt
            ):
                raise _lease_expired()
            # A locking SELECT can have waited on another transaction; the
            # expiry must be compared with a clock read after the row is held.
            await cursor.execute(
                "SELECT lease_expires_at IS NOT NULL "
                "AND clock_timestamp() < lease_expires_at "
                "FROM jobs WHERE id = %s",
                (str(lease.job_id),),
            )
            if not (await cursor.fetchone())[0]:
                raise _lease_expired()

            await cursor.execute(
                "SELECT conversation_id, job_id, actor_id, thread_id, "
                "terminal_at IS NOT NULL, accepted_config "
                "FROM chat_agent_runs WHERE id = %s FOR UPDATE",
                (str(self._run_id),),
            )
            run = await cursor.fetchone()
            if run is None:
                raise _lease_expired()
            conversation_id, job_id, actor_id, run_thread, terminal, accepted = run
            if job_id != lease.job_id or actor_id != actor.id or terminal:
                raise _lease_expired()
            if run_thread != thread_id:
                raise _invalid()

            await cursor.execute(
                "SELECT repository_id FROM chat_conversation_repositories "
                "WHERE conversation_id = %s ORDER BY ordinal",
                (str(conversation_id),),
            )
            members = {str(row[0]) for row in await cursor.fetchall()}
            if members != {str(item) for item in epochs}:
                raise not_found()

        accepted_id = None
        if accepted is not None:
            configurable = (
                accepted.get("configurable") if isinstance(accepted, dict) else None
            )
            if not isinstance(configurable, dict):
                raise _lease_expired()
            accepted_id = _canonical_uuid(
                configurable.get("checkpoint_id"), optional=False
            )
            accepted_id = str(accepted_id)
        return self._select(mode, supplied, accepted_id)

    @staticmethod
    def _select(mode: str, supplied: str | None, accepted_id: str | None) -> str | None:
        if mode == "aput":
            # The official saver treats a config checkpoint id as the parent
            # link; it is legal only when it names the accepted checkpoint.
            if supplied is not None and supplied != accepted_id:
                raise _invalid()
            return accepted_id
        if mode == "writes":
            # A pending write belongs to the accepted checkpoint. Callers may
            # omit the id (the run's accepted config is authoritative) but a
            # different id is never a legal target.
            if accepted_id is None or (
                supplied is not None and supplied != accepted_id
            ):
                raise _invalid()
            return accepted_id
        # Reads are exact: only the accepted checkpoint (or nothing at all on a
        # fresh thread) is ever visible, never an attempt or a stale branch.
        if supplied is None:
            return accepted_id
        if accepted_id is None or supplied != accepted_id:
            raise _invalid()
        return accepted_id

    # ------------------------------------------------------------ accounting

    async def _account(self, cursor, thread: str) -> None:
        await cursor.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread,)
        )
        count = (await cursor.fetchone())[0]
        if count > MAX_CHECKPOINTS_PER_RUN:
            raise _state_limit()
        checkpoint_bytes, metadata_bytes = await (
            await cursor.execute(
                "SELECT coalesce(sum(octet_length(checkpoint::text)), 0), "
                "coalesce(sum(octet_length(metadata::text)), 0) "
                "FROM checkpoints WHERE thread_id = %s",
                (thread,),
            )
        ).fetchone()
        blob_bytes = await self._sum_bytes(cursor, "checkpoint_blobs", thread)
        write_bytes = await self._sum_bytes(cursor, "checkpoint_writes", thread)
        size = checkpoint_bytes + metadata_bytes + blob_bytes + write_bytes
        await cursor.execute(
            "INSERT INTO chat_checkpoint_accounts "
            "(run_id, thread_id, checkpoint_count, bytes, updated_at) "
            "VALUES (%s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            "thread_id = EXCLUDED.thread_id, "
            "checkpoint_count = EXCLUDED.checkpoint_count, "
            "bytes = EXCLUDED.bytes, updated_at = clock_timestamp()",
            (str(self._run_id), thread, count, size),
        )
        await cursor.execute(
            "SELECT coalesce(sum(bytes), 0) FROM chat_checkpoint_accounts"
        )
        if (await cursor.fetchone())[0] <= MAX_GLOBAL_BYTES:
            return
        # Only fully retired terminal threads may be evicted. Active runs, the
        # unknown-budget evidence and the current thread are never candidates.
        await cursor.execute(
            "SELECT a.run_id, a.thread_id FROM chat_checkpoint_accounts a "
            "JOIN chat_agent_runs r ON r.id = a.run_id "
            "WHERE a.run_id <> %s AND r.terminal_at IS NOT NULL "
            "AND r.accepted_retired_at IS NOT NULL "
            "AND r.accepted_config IS NULL "
            "ORDER BY r.terminal_at, r.id",
            (str(self._run_id),),
        )
        candidates = await cursor.fetchall()
        for run_id, retired_thread in candidates:
            await self._saver.adelete_thread(str(retired_thread))
            await cursor.execute(
                "DELETE FROM chat_checkpoint_accounts WHERE run_id = %s",
                (str(run_id),),
            )
            await cursor.execute(
                "SELECT coalesce(sum(bytes), 0) FROM chat_checkpoint_accounts"
            )
            if (await cursor.fetchone())[0] <= MAX_GLOBAL_BYTES:
                return
        raise _state_limit()

    @staticmethod
    async def _sum_bytes(cursor, table: str, thread: str) -> int:
        await cursor.execute(
            f"SELECT coalesce(sum(octet_length(blob)), 0) FROM {table} "
            "WHERE thread_id = %s",
            (thread,),
        )
        return (await cursor.fetchone())[0] or 0


def _raw_dsn(settings: Settings) -> str:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required")
    return settings.database_url.get_secret_value().replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


async def setup_schema(settings: Settings) -> None:
    """Deploy the official checkpoint schema; run as a controlled one-off."""

    async with await psycopg.AsyncConnection.connect(
        _raw_dsn(settings), autocommit=True
    ) as connection:
        await AsyncPostgresSaver(connection).setup()


async def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m devflow.chat.checkpoints")
    parser.add_argument("--setup", action="store_true", required=True)
    parser.parse_args(argv)
    await setup_schema(Settings())


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
