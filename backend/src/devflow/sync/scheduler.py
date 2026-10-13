"""Persist demand and coalesce bounded, contiguous journal prefixes into rounds."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from devflow.audit.service import AuditEntry, write_audit
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.service import append_event
from devflow.repositories.models import Repository
from devflow.sync.models import RepositorySync, SyncCursor, SyncDemand
from devflow.sync.schemas import BackfillRange, SyncDemandInput, SyncTarget
from devflow.sync.scope import authorized, lock_scope, transaction

DEMAND_BATCH = 100


async def enqueue(db, state, installation, repository, *, continued=False):
    now = await db.scalar(select(func.clock_timestamp()))
    if not continued:
        demands = list(
            await db.scalars(
                select(SyncDemand)
                .where(
                    SyncDemand.repository_id == repository.id,
                    SyncDemand.sequence > state.processed_sequence,
                )
                .order_by(SyncDemand.sequence)
                .limit(DEMAND_BATCH)
            )
        )
        if not demands:
            return None
        # Backfills keep their precise windows/streams; do not union unrelated requests.
        if demands[0].backfill is not None:
            demands = demands[:1]
        else:
            for index, item in enumerate(demands):
                if item.backfill is not None:
                    demands = demands[:index]
                    break
        state.generation += 1
        state.request_count = 0
        state.round_started_at = now
        captured = demands[-1].sequence
    else:
        old = await db.get(Job, state.active_job_id)
        captured = old.input["captured_sequence"]
    value = {
        "generation": state.generation,
        "captured_sequence": captured,
        "installation_id": installation.installation_id,
        "installation_version": installation.auth_version,
        "repository_version": repository.auth_version,
    }
    job = Job(
        kind="repository.sync",
        repository_id=repository.id,
        requested_by=None,
        input=value,
        input_hash=hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest(),
        idempotency_key=f"sync:{state.generation}:{uuid4().hex}",
        available_at=now,
    )
    db.add(job)
    await db.flush()
    state.active_job_id = job.id
    state.status = "queued"
    await append_event(db, job, "queued")
    await write_audit(
        db,
        AuditEntry(
            action="job.submitted",
            resource_type="job",
            resource_id=job.id,
            job_id=job.id,
            outcome="success",
        ),
    )
    return job


async def request_sync(
    db,
    repository_id,
    *,
    reason: str,
    target: SyncTarget | None = None,
    backfill: BackfillRange | None = None,
    resume: bool = False,
) -> RepositorySync:
    # Validate before taking locks. The caller owns actor checks and the transaction.
    value = SyncDemandInput(
        repository_id=repository_id,
        sequence=1,
        reason=reason,
        target=target,
        backfill=backfill,
    )
    if backfill and backfill.until - backfill.since > timedelta(days=365):
        raise AppError("validation_error", 422, "Backfill exceeds 365 days")
    rows = await lock_scope(db, repository_id)
    if rows is None or not authorized(rows[0], rows[1], None):
        raise AppError("resource_not_found", 404, "Repository unavailable")
    installation, repository, state = rows
    if state is None:
        state = RepositorySync(repository_id=repository_id)
        db.add(state)
        await db.flush()
    state.requested_sequence += 1
    db.add(
        SyncDemand.from_input(
            value.model_copy(update={"sequence": state.requested_sequence})
        )
    )
    await db.flush()
    # Only explicit admin resume restarts an exhausted round.
    can_start = (
        resume
        if state.status == "budget_exhausted"
        else (
            state.status != "failed"
            or reason in {"refresh", "backfill", "registration"}
        )
    )
    if state.active_job_id is None and can_start:
        cursors = list(
            await db.scalars(
                select(SyncCursor).where(SyncCursor.repository_id == repository_id)
            )
        )
        resumable = state.status in {"failed", "budget_exhausted"} and any(
            c.status != "complete" for c in cursors
        )
        if resumable:
            previous = await db.scalar(
                select(Job)
                .where(
                    Job.repository_id == repository_id,
                    Job.kind == "repository.sync",
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            state.active_job_id = previous.id
            state.request_count = 0
            state.round_started_at = await db.scalar(select(func.clock_timestamp()))
            await enqueue(db, state, installation, repository, continued=True)
        else:
            await enqueue(db, state, installation, repository)
    await write_audit(
        db,
        AuditEntry(
            action="backfill.requested" if backfill else "sync.requested",
            resource_type="repository",
            resource_id=repository_id,
            outcome="success",
        ),
    )
    return state


class SyncScheduler:
    def __init__(self, factory, settings):
        self.factory, self.settings = factory, settings

    async def tick(self) -> int:
        async with transaction(self.factory) as db:
            ids = list(
                await db.scalars(
                    select(Repository.id)
                    .outerjoin(RepositorySync)
                    .where(
                        Repository.enabled.is_(True),
                        Repository.available.is_(True),
                        Repository.needs_verification.is_(False),
                        RepositorySync.active_job_id.is_(None),
                        (
                            RepositorySync.status.is_(None)
                            | RepositorySync.status.in_(("idle", "completed"))
                        ),
                        (
                            RepositorySync.next_due_at.is_(None)
                            | (RepositorySync.next_due_at <= func.clock_timestamp())
                        ),
                    )
                    .order_by(
                        RepositorySync.next_due_at.asc().nullsfirst(), Repository.id
                    )
                    .limit(100)
                )
            )
        scheduled = 0
        for identity in ids:
            async with transaction(self.factory) as db:
                rows = await lock_scope(db, identity, skip_locked=True)
                if rows is None or not authorized(rows[0], rows[1], self.settings):
                    continue
                state = rows[2]
                now = await db.scalar(select(func.clock_timestamp()))
                if state and (
                    state.active_job_id
                    or state.status not in {"idle", "completed"}
                    or (state.next_due_at and state.next_due_at > now)
                ):
                    continue
                reason = (
                    "reconcile"
                    if state is None
                    or state.last_reconciled_at is None
                    or state.last_reconciled_at
                    + timedelta(seconds=self.settings.sync_reconcile_seconds)
                    <= now
                    else "periodic"
                )
                await request_sync(db, identity, reason=reason)
                scheduled += 1
        return scheduled
