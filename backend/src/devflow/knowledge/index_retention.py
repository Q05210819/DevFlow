"""Bounded physical reclamation of failed, superseded, and expired vectors.

Metadata is the authority: an index row is never deleted by this sweep, only
marked as purged after its Milvus version has been removed. Reclaiming vectors
therefore cannot erase the provenance of citations that still reference an
index identity or its canonical document text.

The sweep deliberately does not finalize the cleanup Job. ``KnowledgeRetention``
runs after it for the same lease and owns the Job's terminal transition; this
sweep only fences itself under the lease for its own writes.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.queue import Queue
from devflow.jobs.schemas import KnowledgeCleanupInput
from devflow.knowledge.models import (
    IndexVersion,
    KnowledgeCapacity,
    KnowledgeCorpus,
    RepositoryCapacity,
)
from devflow.sync.scope import transaction

GLOBAL_MEMBER_CAP = 120_000
RETIRED_TTL_DAYS = 7
CLEANUP_BATCH = 100
PROTECTED = frozenset({"queued", "building", "ready"})


@dataclass
class IndexRetentionResult:
    examined: int = 0
    purged: int = 0
    deferred: int = 0


def _retired_at(row: IndexVersion):
    return row.retired_at or row.created_at


class IndexRetention:
    """Purge at most one bounded batch of non-queryable index versions."""

    def __init__(self, factory, *, index):
        self.factory, self.index = factory, index

    async def sweep(self, lease, *, limit: int = CLEANUP_BATCH) -> IndexRetentionResult:
        if type(limit) is not int or not 1 <= limit <= CLEANUP_BATCH:
            raise AppError("validation_error", 422, "Invalid cleanup limit")
        result = IndexRetentionResult()
        # Binding hint only; it is rechecked under the owned Job lock below.
        async with self.factory() as hint:
            repository_id = await hint.scalar(
                select(Job.repository_id).where(Job.id == lease.job_id)
            )
        if repository_id is None:
            return result
        selected: list[IndexVersion] = []
        async with transaction(self.factory) as db:
            await db.execute(
                pg_insert(KnowledgeCapacity).values(id=1).on_conflict_do_nothing()
            )
            await db.get(KnowledgeCapacity, 1, with_for_update=True)
            await db.execute(
                pg_insert(RepositoryCapacity)
                .values(repository_id=repository_id)
                .on_conflict_do_nothing()
            )
            await db.get(RepositoryCapacity, repository_id, with_for_update=True)
            job, now = await Queue(self.factory)._owned(db, lease)
            if job is None:
                return result
            if job.kind != "knowledge.cleanup" or job.repository_id != repository_id:
                raise AppError("validation_error", 422, "Not a cleanup job")
            payload = KnowledgeCleanupInput.model_validate(job.input)
            corpus = await db.get(KnowledgeCorpus, repository_id, with_for_update=True)
            active_id = None if corpus is None else corpus.active_index_version_id
            rows = list(
                await db.scalars(
                    select(IndexVersion)
                    .where(
                        IndexVersion.repository_id == repository_id,
                        IndexVersion.purged_at.is_(None),
                    )
                    .order_by(IndexVersion.created_at, IndexVersion.id)
                    .with_for_update()
                )
            )
            result.examined = len(rows)
            total = int(
                await db.scalar(
                    select(
                        func.coalesce(func.sum(IndexVersion.written_count), 0)
                    ).where(IndexVersion.purged_at.is_(None))
                )
                or 0
            )
            selected = self._candidates(rows, active_id, now, total)[
                : min(limit, payload.limit)
            ]
            for row in selected:
                # Invisible immediately, even before the external delete and
                # the purged marker: a crash can only cause a safe retry.
                row.queryable = False
                row.updated_at = now
            selected = list(selected)
        purged_ids, deferred = await self._delete_versions(selected)
        result.deferred += deferred
        async with transaction(self.factory) as db:
            job, now = await Queue(self.factory)._owned(db, lease)
            if job is None:
                # The lease moved on while external deletes ran; the rows stay
                # non-queryable and unpurged so a later sweep retries them.
                result.deferred += len(purged_ids)
                return result
            rows = list(
                await db.scalars(
                    select(IndexVersion)
                    .where(IndexVersion.id.in_(purged_ids))
                    .order_by(IndexVersion.id)
                    .with_for_update()
                )
            )
            for row in rows:
                if row.purged_at is None:
                    row.purged_at = now
                    row.queryable = False
                    row.updated_at = now
            result.purged = len(rows)
        return result

    @staticmethod
    def _candidates(rows, active_id, now, total_members) -> list[IndexVersion]:
        cutoff = now - timedelta(days=RETIRED_TTL_DAYS)
        forced: dict[UUID, IndexVersion] = {}
        reserve: list[IndexVersion] = []
        for row in rows:
            if row.id == active_id or row.status in PROTECTED:
                continue
            if row.status == "retired":
                if _retired_at(row) < cutoff:
                    forced[row.id] = row
                else:
                    reserve.append(row)
            else:
                # Superseded and failed builds can never become queryable
                # again, so their vectors are reclaimed as soon as the sweep
                # sees them.
                forced[row.id] = row
        # At most the single newest still-fresh retired version is retained,
        # and only while the global member budget allows it. Newest is by the
        # time the version actually retired, not by its original build start.
        reserve.sort(key=lambda row: (_retired_at(row), row.id), reverse=True)
        forced.update((row.id, row) for row in reserve[1:])
        remaining = total_members - sum(row.written_count for row in forced.values())
        if remaining > GLOBAL_MEMBER_CAP:
            for row in reversed(reserve):
                if remaining <= GLOBAL_MEMBER_CAP:
                    break
                if row.id in forced:
                    continue
                forced[row.id] = row
                remaining -= row.written_count
        return sorted(forced.values(), key=lambda row: (row.created_at, row.id))

    async def _delete_versions(self, rows) -> tuple[list[UUID], int]:
        purged: list[UUID] = []
        deferred = 0
        for row in rows:
            try:
                # A failed or crashed batch can leave vectors behind before its
                # written_count is recorded, so every selected version is
                # purged rather than trusting the counter. The adapter never
                # creates the collection here: if it is absent, the vectors are
                # already gone and the version still counts as purged.
                await asyncio.to_thread(self.index.purge_version, row.id)
            except Exception:  # noqa: BLE001 - a failed delete stays retryable
                deferred += 1
            else:
                purged.append(row.id)
        return purged, deferred
