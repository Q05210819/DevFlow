from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.jobs.events import JobEventService, parse_cursor
from devflow.jobs.schemas import IdempotencyKey, JobView
from devflow.jobs.service import JobService

router = APIRouter(prefix="/api/v1")


def get_job_service(request: Request) -> JobService:
    service = getattr(request.app.state, "job_service", None)
    if service is None:
        raise AppError("service_unavailable", 503, "Service unavailable")
    return service


@router.post(
    "/repositories/{repository_id}/access-checks",
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
async def submit_probe(
    repository_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> JobView:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await get_job_service(request).submit_probe(
            actor, repository_id, idempotency_key
        )
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("/jobs/{job_id}")
async def get_visible(
    job_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
) -> JobView:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await get_job_service(request).get_visible(actor, job_id)
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.post(
    "/jobs/{job_id}/cancel",
    dependencies=[Depends(require_csrf)],
)
async def cancel_job(
    job_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> JobView:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await get_job_service(request).cancel(actor, job_id, idempotency_key)
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("/jobs/{job_id}/events")
async def stream_events(
    job_id: UUID,
    request: Request,
) -> StreamingResponse:
    service = JobEventService(get_job_service(request))
    try:
        actor = await require_actor(request)
        job, _ = await service._authorize(request, actor, job_id)
        # Choose first, then parse: a malformed unused query must be ignored.
        raw = request.headers.get("Last-Event-ID")
        if raw is None:
            raw = request.query_params.get("after", "0")
        after = parse_cursor(raw, job.last_sequence)
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None
    return StreamingResponse(
        service.stream_job_events(request, actor, job_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
