"""Durable queue operations use independent PostgreSQL transactions."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.models import User
from devflow.jobs.models import Job
from devflow.jobs.schemas import HealthResult, JobErrorCode, ProbeResult, SyncResult
from devflow.jobs.service import append_event
from devflow.logging import logger

_ERROR = TypeAdapter(JobErrorCode)


@dataclass(frozen=True, slots=True)
class Lease:
    job_id: UUID
    owner: UUID
    generation: int
    attempt: int


class Queue:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int = 30,
        retry_first_seconds: int = 10,
        retry_second_seconds: int = 30,
    ):
        self.factory = factory
        self.lease_seconds = lease_seconds
        self.backoffs = (retry_first_seconds, retry_second_seconds)

    @asynccontextmanager
    async def _transaction(self):
        # Bound the entire operation as well as PostgreSQL waits. No HTTP is
        # performed in these transactions and every heartbeat gets its own session.
        async with asyncio.timeout(5), self.factory.begin() as session:
            await session.execute(text("SET LOCAL statement_timeout = '4s'"))
            await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            yield session

    async def claim(self, owner: UUID) -> Lease | None:
        async with self._transaction() as session:
            job = await session.scalar(
                select(Job)
                .where(
                    Job.status.in_(("queued", "retry_wait")),
                    Job.available_at <= func.clock_timestamp(),
                    Job.attempt < Job.max_attempts,
                )
                .order_by(Job.available_at, Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            now = await session.scalar(select(func.clock_timestamp()))
            # The database clock may move between statements.
            if job.available_at > now:
                return None
            wait = max(0, (now - job.available_at).total_seconds())
            job.status = "running"
            job.attempt += 1
            job.lease_generation += 1
            job.lease_owner = owner
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.updated_at = now
            job.error_code = None
            await self._transition(session, job, "job.started")
            lease = Lease(job.id, owner, job.lease_generation, job.attempt)
        _metric("queue.claim", lease, queue_wait_seconds=wait)
        return lease

    async def _owned(self, session, lease):
        job = await session.scalar(
            select(Job)
            .where(
                Job.id == lease.job_id,
                Job.lease_owner == lease.owner,
                Job.lease_generation == lease.generation,
                Job.status == "running",
                Job.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        # Recheck after acquisition: a SELECT can have waited on a row lock.
        if job is None or job.lease_expires_at <= now:
            return None, now
        return job, now

    async def heartbeat(self, lease: Lease) -> bool:
        async with self._transaction() as session:
            job, now = await self._owned(session, lease)
            if job is None:
                return False
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.updated_at = now
        _metric("queue.heartbeat", lease)
        return True

    async def complete(
        self, lease: Lease, result: ProbeResult | SyncResult | HealthResult
    ) -> bool:
        async with self._transaction() as session:
            job, now = await self._owned(session, lease)
            if job is None:
                return False
            schema = {
                "repository.access_probe": ProbeResult,
                "repository.sync": SyncResult,
                "repository.health": HealthResult,
            }[job.kind]
            validated = schema.model_validate(result)
            job.status = "succeeded"
            job.result = validated.model_dump(mode="json")
            job.error_code = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            await self._transition(session, job, "job.succeeded")
        _metric("queue.complete", lease)
        return True

    async def fail(
        self,
        lease: Lease,
        code: str,
        retryable: bool,
        retry_at: datetime | None = None,
    ) -> bool:
        try:
            code = _ERROR.validate_python(code)
        except ValidationError:
            code, retryable = "internal_error", False
        async with self._transaction() as session:
            denial = code in {
                "resource_not_found",
                "permission_revoked",
                "permission_missing",
                "github_installation_unavailable",
            }
            if denial:
                # Read the immutable creator without taking Job's lock, then
                # acquire the actor FK lock in the established User → Job order.
                creator = await session.scalar(
                    select(Job.requested_by).where(Job.id == lease.job_id)
                )
                if creator is not None:
                    await session.execute(
                        select(User.id)
                        .where(User.id == creator)
                        .with_for_update(read=True, key_share=True)
                    )
            job, now = await self._owned(session, lease)
            if job is None:
                return False
            await self._failure(session, job, now, code, retryable, retry_at)
            if denial:
                await write_audit(
                    session,
                    AuditEntry(
                        actor_id=job.requested_by,
                        action="permission.denied",
                        resource_type="repository",
                        resource_id=job.repository_id,
                        job_id=job.id,
                        outcome="failure",
                        reason_code=code,
                    ),
                )
        _metric("queue.failure", lease, error_code=code)
        return True

    async def recover_expired(self) -> int:
        recovered = []
        async with self._transaction() as session:
            jobs = await session.scalars(
                select(Job)
                .where(
                    Job.status == "running",
                    Job.lease_expires_at <= func.clock_timestamp(),
                )
                .order_by(Job.lease_expires_at, Job.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for job in jobs:
                now = await session.scalar(select(func.clock_timestamp()))
                if job.lease_expires_at > now:
                    continue
                old = Lease(job.id, job.lease_owner, job.lease_generation, job.attempt)
                job.lease_generation += 1
                await self._failure(session, job, now, "lease_expired", True, None)
                recovered.append(old)
        for lease in recovered:
            _metric("queue.recovered", lease, error_code="lease_expired")
        return len(recovered)

    async def _failure(self, session, job, now, code, retryable, retry_at):
        retry = retryable and job.attempt < job.max_attempts
        job.status = "retry_wait" if retry else "failed"
        job.result = None
        job.error_code = code
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        if retry:
            deadline = now + timedelta(seconds=self.backoffs[job.attempt - 1])
            job.available_at = max(deadline, retry_at) if retry_at else deadline
        await self._transition(session, job, "job.retried" if retry else "job.failed")

    @staticmethod
    async def _transition(session, job, action):
        await append_event(session, job, job.status, job.error_code)
        # A system worker is the actor. Referencing requested_by here would
        # acquire User key-share through the audit FK while holding Job, reversing
        # the submission path's User → Job order. The creator stays on Job.
        await write_audit(
            session,
            AuditEntry(
                actor_id=None,
                action=action,
                resource_type="job",
                resource_id=job.id,
                job_id=job.id,
                outcome="failure" if job.error_code else "success",
                reason_code=job.error_code,
            ),
        )


def _metric(action, lease, *, error_code=None, queue_wait_seconds=None):
    fields = {
        "action": action,
        "job_id": str(lease.job_id),
        "attempt": lease.attempt,
        "generation": lease.generation,
        "count": 1,
    }
    if error_code is not None:
        fields["error_code"] = error_code
    if queue_wait_seconds is not None:
        fields["queue_wait_seconds"] = queue_wait_seconds
    logger.info(json.dumps(fields, separators=(",", ":")))
