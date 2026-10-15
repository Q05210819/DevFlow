"""Authorized read model and idempotent commands for shared repository sync."""

import math
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from devflow.audit.service import AuditEntry, write_audit
from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.service import forbidden
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.jobs.models import Job
from devflow.jobs.routes import get_job_service
from devflow.jobs.schemas import IdempotencyKey
from devflow.repositories.policy import check_actor
from devflow.repositories.service import command_receipt
from devflow.sync.models import RepositoryCommandReceipt, RepositorySync, SyncCursor
from devflow.sync.scheduler import request_sync
from devflow.sync.schemas import (
    BackfillRange,
    SyncBackfill,
    SyncCheckpoint,
    SyncCoverage,
    SyncRefresh,
    SyncRemaining,
    SyncStreamView,
    SyncView,
)
from devflow.sync.scope import transaction

router = APIRouter(prefix="/api/v1/repositories/{repository_id}/sync", tags=["sync"])


async def sync_view(db, repository_id):
    # Caller holds Repository, then SyncState, then reads the active Job.
    state = await db.scalar(
        select(RepositorySync)
        .where(RepositorySync.repository_id == repository_id)
        .with_for_update()
    )
    cursors = {
        row.stream: row
        for row in await db.scalars(
            select(SyncCursor).where(SyncCursor.repository_id == repository_id)
        )
    }
    streams = []
    for stream in ("issues", "pulls", "actions"):
        cursor = cursors.get(stream)
        checkpoint = (
            SyncCheckpoint.model_validate(cursor.checkpoint) if cursor else None
        )
        remaining = None
        if checkpoint:
            remaining = SyncRemaining(
                pending_targets=len(checkpoint.pending_targets),
                pending_windows=len(checkpoint.pending_windows)
                + int(checkpoint.active_window is not None),
                unresolved_windows=len(checkpoint.unresolved_windows),
                current_target=checkpoint.target is not None,
                enumeration_complete=True if cursor.status == "complete" else None,
                mode=checkpoint.mode,
                stage=checkpoint.stage,
                reason=checkpoint.gap_reason,
            )
        streams.append(
            SyncStreamView(
                stream=stream,
                status=cursor.status if cursor else "not_synced",
                coverage=SyncCoverage(
                    start=cursor.covered_from,
                    end=cursor.covered_until,
                    completed_watermark=cursor.completed_watermark,
                )
                if cursor
                else SyncCoverage(),
                last_success_at=cursor.last_success_at if cursor else None,
                remaining=remaining,
                error_code=cursor.error_code if cursor else None,
            )
        )
    job = (
        await db.get(Job, state.active_job_id)
        if state and state.active_job_id
        else None
    )
    if state and state.status in {"failed", "budget_exhausted"} and job is None:
        job = await db.scalar(
            select(Job)
            .where(Job.repository_id == repository_id, Job.kind == "repository.sync")
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
    error = (
        "budget_exhausted"
        if state and state.status == "budget_exhausted"
        else job.error_code
        if job
        else None
    )
    if error is None:
        error = next(
            (stream.error_code for stream in streams if stream.error_code), None
        )
    return SyncView(
        repository_id=repository_id,
        requested_sequence=state.requested_sequence if state else 0,
        processed_sequence=state.processed_sequence if state else 0,
        generation=state.generation if state else 0,
        status=state.status if state else "idle",
        streams=streams,
        error_code=error,
        next_due_at=state.next_due_at if state else None,
        retry_at=job.available_at if job and job.status == "retry_wait" else None,
    )


async def resource(request, response, actor, repository_id, *, body=None, key=None):
    jobs = get_job_service(request)
    operation = "sync.backfill" if isinstance(body, SyncBackfill) else "sync.refresh"
    admin = (
        isinstance(body, SyncBackfill) or isinstance(body, SyncRefresh) and body.resume
    )
    snapshot = None
    try:
        if admin and actor.role != "admin":
            raise forbidden()
        snapshot = await jobs._authorize(actor, repository_id, fresh=body is not None)
        async with transaction(jobs.factory) as db:
            await jobs._lock_authorization(db, actor, snapshot)
            if admin and (await check_actor(db, actor)).role != "admin":
                raise forbidden()
            if body is not None:
                payload = body.model_dump(mode="json")
                if isinstance(body, SyncBackfill):
                    payload["streams"] = sorted(payload["streams"])
                receipt, payload_hash = await command_receipt(
                    db,
                    actor_id=actor.id,
                    repository_id=repository_id,
                    operation=operation,
                    idempotency_key=key,
                    normalized_payload=payload,
                )
                if receipt is None:
                    backfill = None
                    if isinstance(body, SyncBackfill):
                        now = await db.scalar(select(func.clock_timestamp()))
                        if body.end > now:
                            raise AppError(
                                "validation_error",
                                422,
                                "Backfill cannot end in the future",
                            )
                        backfill = BackfillRange(
                            since=body.start,
                            until=body.end,
                            streams=sorted(body.streams),
                        )
                    state = await request_sync(
                        db,
                        repository_id,
                        reason="backfill" if backfill else "refresh",
                        backfill=backfill,
                        resume=isinstance(body, SyncRefresh) and body.resume,
                        actor_id=actor.id,
                    )
                    db.add(
                        RepositoryCommandReceipt(
                            actor_id=actor.id,
                            repository_id=repository_id,
                            operation=operation,
                            idempotency_key=key,
                            payload_hash=payload_hash,
                            demand_sequence=state.requested_sequence,
                        )
                    )
                    await db.flush()
            view = await sync_view(db, repository_id)
            if view.retry_at is not None:
                now = await db.scalar(select(func.clock_timestamp()))
                response.headers["Retry-After"] = str(
                    max(1, math.ceil((view.retry_at - now).total_seconds()))
                )
        response.headers["Cache-Control"] = "no-store"
        return view
    except AppError as error:
        # _authorize audits access denials. Only commands rejected before it or
        # transaction-time revocation need another denial audit here.
        if error.status == 409:
            async with transaction(jobs.factory) as db:
                await write_audit(
                    db,
                    AuditEntry(
                        actor_id=actor.id,
                        action="backfill.requested"
                        if operation == "sync.backfill"
                        else "sync.requested",
                        resource_type="repository",
                        resource_id=repository_id,
                        outcome="failure",
                        reason_code="idempotency_conflict",
                    ),
                )
        elif snapshot is not None or admin and actor.role != "admin":
            await jobs.policy.audit_denial(actor, error, repository_id)
        raise
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("")
async def get_sync(
    repository_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
) -> SyncView:
    return await resource(request, response, actor, repository_id)


@router.post("/refresh", status_code=202, dependencies=[Depends(require_csrf)])
async def refresh(
    repository_id: UUID,
    request: Request,
    response: Response,
    body: SyncRefresh,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> SyncView:
    return await resource(
        request, response, actor, repository_id, body=body, key=idempotency_key
    )


@router.post("/backfills", status_code=202, dependencies=[Depends(require_csrf)])
async def backfill(
    repository_id: UUID,
    request: Request,
    response: Response,
    body: SyncBackfill,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> SyncView:
    return await resource(
        request, response, actor, repository_id, body=body, key=idempotency_key
    )
