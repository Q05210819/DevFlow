from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.health.schemas import HealthRefresh, HealthView
from devflow.health.service import HealthService
from devflow.jobs.schemas import IdempotencyKey
from devflow.repositories.routes import get_repository_service

router = APIRouter(
    prefix="/api/v1/repositories/{repository_id}/health", tags=["health"]
)


async def resource(request, response, actor, repo, key=None):
    repositories = get_repository_service(request)
    service = HealthService(
        repositories.factory, repositories.policy, request.app.state.settings
    )
    response.headers["Cache-Control"] = "no-store"
    try:
        return (
            await service.request(actor, repo, key)
            if key
            else await service.latest(actor, repo)
        )
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("")
async def latest(
    repository_id: UUID,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
) -> HealthView:
    return await resource(request, response, actor, repository_id)


@router.post("/refresh", status_code=202, dependencies=[Depends(require_csrf)])
async def refresh(
    repository_id: UUID,
    request: Request,
    response: Response,
    body: HealthRefresh,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> HealthView:
    return await resource(request, response, actor, repository_id, idempotency_key)
