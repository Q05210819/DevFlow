from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.facts.schemas import CollectionKind, FactDetail, FactPage
from devflow.facts.service import FactService
from devflow.repositories.routes import get_repository_service

router = APIRouter(prefix="/api/v1/repositories/{repository_id}", tags=["facts"])
Limit = Annotated[int, Query(ge=1, le=100)]


def service(request):
    repositories = get_repository_service(request)
    return FactService(repositories.factory, repositories.policy, repositories.codec())


async def read(request, response, actor, repository_id, kind, identity=None, **kwargs):
    response.headers["Cache-Control"] = "no-store"
    try:
        facts = service(request)
        if identity is None:
            return await facts.list(actor, repository_id, kind, **kwargs)
        return await facts.detail(actor, repository_id, kind, identity, **kwargs)
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


def list_endpoint(kind):
    async def endpoint(
        repository_id: UUID,
        request: Request,
        response: Response,
        actor: Annotated[Actor, Depends(require_actor)],
        limit: Limit = 30,
        cursor: str | None = None,
        state: Literal["open", "closed", "unknown"] | None = None,
        head_sha: Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}$")] = None,
        branch: Annotated[str | None, Query(max_length=255)] = None,
    ) -> FactPage:
        return await read(
            request,
            response,
            actor,
            repository_id,
            kind,
            limit=limit,
            cursor=cursor,
            filters={"state": state, "head_sha": head_sha, "branch": branch},
        )

    return endpoint


def detail_endpoint(kind):
    async def endpoint(
        repository_id: UUID,
        number_or_id: int,
        request: Request,
        response: Response,
        actor: Annotated[Actor, Depends(require_actor)],
        collection: CollectionKind | None = None,
        limit: Limit = 30,
        cursor: str | None = None,
    ) -> FactDetail | FactPage:
        return await read(
            request,
            response,
            actor,
            repository_id,
            kind,
            number_or_id,
            collection=collection,
            limit=limit,
            cursor=cursor,
        )

    return endpoint


for path, kind in (("issues", "issue"), ("pulls", "pull"), ("actions/runs", "ci_run")):
    router.add_api_route(
        "/" + path, list_endpoint(kind), methods=["GET"], name=f"list_{kind}"
    )
    if kind != "ci_run":
        router.add_api_route(
            "/" + path + "/{number_or_id}",
            detail_endpoint(kind),
            methods=["GET"],
            name=f"detail_{kind}",
        )


@router.get("/actions/runs/{run_id}/attempts/{attempt}")
async def run_detail(
    repository_id: UUID,
    run_id: int,
    attempt: int,
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
    collection: CollectionKind | None = None,
    limit: Limit = 30,
    cursor: str | None = None,
) -> FactDetail | FactPage:
    return await read(
        request,
        response,
        actor,
        repository_id,
        "ci_run",
        run_id,
        attempt=attempt,
        collection=collection,
        limit=limit,
        cursor=cursor,
    )
