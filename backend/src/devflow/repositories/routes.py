from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from devflow.auth.dependencies import require_actor, require_admin, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.pagination import Page
from devflow.repositories.schemas import (
    CandidateRepository,
    RepositoryRegister,
    RepositoryView,
)
from devflow.repositories.service import RepositoryService

router = APIRouter(prefix="/api/v1")


def get_repository_service(request: Request) -> RepositoryService:
    service = getattr(request.app.state, "repository_service", None)
    if service is None:
        raise AppError("service_unavailable", 503, "Service unavailable")
    return service


@router.get("/github/repositories")
async def discover(
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> Page[CandidateRepository]:
    response.headers["Cache-Control"] = "no-store"
    return await get_repository_service(request).discover(actor, limit, cursor)


@router.post("/repositories", dependencies=[Depends(require_csrf)])
async def register(
    request: Request,
    response: Response,
    body: RepositoryRegister,
    actor: Annotated[Actor, Depends(require_admin)],
) -> RepositoryView:
    view, created = await get_repository_service(request).register_result(
        actor, body.installation_id, body.github_repository_id
    )
    response.status_code = 201 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get("/repositories")
async def list_enabled(
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> Page[RepositoryView]:
    response.headers["Cache-Control"] = "no-store"
    return await get_repository_service(request).list_enabled(actor, limit, cursor)
