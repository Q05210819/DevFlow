from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
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
