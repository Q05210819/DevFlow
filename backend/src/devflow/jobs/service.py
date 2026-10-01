"""Authorized durable submissions. Network calls never hold job transaction locks."""

import hashlib
import json
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.github.client import not_found
from devflow.github.models import GitHubInstallation
from devflow.jobs.models import Job, RunEvent
from devflow.jobs.schemas import EventPayload, IdempotencyKey, JobStatus, JobView
from devflow.repositories.models import Repository
from devflow.repositories.policy import RepositoryPolicy, check_actor

_KEY = TypeAdapter(IdempotencyKey)
_KIND = "repository.access_probe"


async def append_event(
    session: AsyncSession, job: Job, status: JobStatus, error_code: str | None = None
) -> RunEvent:
    """Append under the job lock; caller owns the state, audit and commit.

    The Job must already be flushed. Read only the stored sequence so pending
    caller changes to status/attempt/result survive the lock acquisition.
    """
    payload = EventPayload(status=status, attempt=job.attempt, error_code=error_code)
    with session.no_autoflush:
        sequence = await session.scalar(
            select(Job.last_sequence).where(Job.id == job.id).with_for_update()
        )
    if sequence is None:
        raise ValueError("Event requires a persisted job")
    job.last_sequence = sequence + 1
    event = RunEvent(
        job_id=job.id,
        sequence=job.last_sequence,
        event="job.status",
        payload=payload.model_dump(),
    )
    session.add(event)
    await session.flush()
    return event


class JobService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        policy: RepositoryPolicy,
        *,
        max_attempts: int = 3,
    ):
        self.factory, self.policy = factory, policy
        self.max_attempts = max_attempts

    async def _authorize(self, actor: Actor, repository_id: UUID, *, fresh: bool):
        try:
            _, key = await self.policy.local(actor, repository_id)
        except AppError as error:
            await self.policy.audit_denial(actor, error, repository_id)
            raise
        await self.policy.require(actor, repository_id, fresh=fresh)
        return key

    async def _lock_authorization(
        self, session: AsyncSession, actor: Actor, key: tuple
    ):
        # Same lock order as repository registration and lifecycle writers.
        user = await check_actor(session, actor, lock=True)
        installation = await session.scalar(
            select(GitHubInstallation)
            .where(GitHubInstallation.installation_id == key[5])
            .with_for_update()
        )
        repository = await session.scalar(
            select(Repository).where(Repository.id == key[1]).with_for_update()
        )
        if (
            installation is None
            or repository is None
            or not installation.available
            or installation.needs_verification
            or not repository.enabled
            or not repository.available
            or repository.needs_verification
            or not self.policy.allowed(installation.installation_id)
            or repository.installation_id != installation.installation_id
            or key
            != (
                actor.id,
                repository.id,
                user.auth_version,
                installation.auth_version,
                repository.auth_version,
                installation.installation_id,
            )
        ):
            raise not_found()

    async def submit_probe(
        self, actor: Actor, repository_id: UUID, idempotency_key: str
    ) -> JobView:
        try:
            key = _KEY.validate_python(idempotency_key)
        except ValidationError:
            raise AppError("validation_error", 422, "Invalid request") from None
        snapshot = await self._authorize(actor, repository_id, fresh=True)
        input_data = {"repository_id": str(repository_id)}
        input_hash = hashlib.sha256(
            json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            async with self.factory.begin() as session:
                await self._lock_authorization(session, actor, snapshot)
                job = await session.scalar(
                    insert(Job)
                    .values(
                        kind=_KIND,
                        repository_id=repository_id,
                        requested_by=actor.id,
                        input=input_data,
                        input_hash=input_hash,
                        idempotency_key=key,
                        max_attempts=self.max_attempts,
                    )
                    .on_conflict_do_nothing(constraint="uq_jobs_creator_kind_key")
                    .returning(Job)
                )
                if job is None:
                    job = await session.scalar(
                        select(Job).where(
                            Job.requested_by == actor.id,
                            Job.kind == _KIND,
                            Job.idempotency_key == key,
                        )
                    )
                    if job.input_hash != input_hash:
                        raise AppError(
                            "idempotency_conflict",
                            409,
                            "Idempotency key conflicts with input",
                        )
                else:
                    await append_event(session, job, "queued")
                    await write_audit(
                        session,
                        AuditEntry(
                            actor_id=actor.id,
                            action="job.submitted",
                            resource_type="job",
                            resource_id=job.id,
                            job_id=job.id,
                            outcome="success",
                        ),
                    )
                view = JobView.model_validate(job)
            return view
        except AppError as error:
            if error.status == 409:
                async with self.factory.begin() as session:
                    await write_audit(
                        session,
                        AuditEntry(
                            actor_id=actor.id,
                            action="job.submitted",
                            resource_type="job",
                            outcome="failure",
                            reason_code="idempotency_conflict",
                        ),
                    )
            else:
                await self.policy.audit_denial(actor, error, repository_id)
            raise

    async def get_visible(self, actor: Actor, job_id: UUID) -> JobView:
        # Load only the creator-scoped authorization reference, never the result.
        async with self.factory() as session:
            repository_id = await session.scalar(
                select(Job.repository_id).where(
                    Job.id == job_id, Job.requested_by == actor.id
                )
            )
        if repository_id is None:
            async with self.factory.begin() as session:
                await check_actor(session, actor)
                await write_audit(
                    session,
                    AuditEntry(
                        actor_id=actor.id,
                        action="permission.denied",
                        resource_type="job",
                        resource_id=job_id,
                        outcome="failure",
                        reason_code="resource_not_found",
                    ),
                )
            raise not_found()
        snapshot = await self._authorize(actor, repository_id, fresh=False)
        try:
            async with self.factory.begin() as session:
                await self._lock_authorization(session, actor, snapshot)
                job = await session.scalar(
                    select(Job).where(Job.id == job_id, Job.requested_by == actor.id)
                )
                if job is None:
                    raise not_found()
                return JobView.model_validate(job)
        except AppError as error:
            await self.policy.audit_denial(actor, error, repository_id)
            raise
