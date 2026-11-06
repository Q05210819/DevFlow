"""Repository-scoped immutable workspace metadata and bounded code reads."""

from typing import Annotated
from urllib.parse import unquote_to_bytes
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from devflow.auth.dependencies import require_actor, require_csrf
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.jobs.schemas import IdempotencyKey
from devflow.repositories.routes import get_repository_service
from devflow.workspace.read import WorkspaceReader
from devflow.workspace.schemas import (
    FileView,
    SearchView,
    TreePage,
    WorkspacePage,
    WorkspaceSelection,
    WorkspaceView,
)
from devflow.workspace.service import WorkspaceService

router = APIRouter(
    prefix="/api/v1/repositories/{repository_id}/workspaces", tags=["workspaces"]
)


async def invoke(request, method, actor, repository_id, workspace_id=None, **kwargs):
    repositories = get_repository_service(request)
    try:
        # Starlette replaces malformed UTF-8 query bytes with U+FFFD. Refuse the
        # malformed transport instead of letting it select a different Git name.
        try:
            unquote_to_bytes(request.scope["query_string"]).decode("utf-8")
        except UnicodeError:
            raise AppError("validation_error", 422, "Invalid query encoding") from None
        if method in {"tree", "file", "search"}:
            service = WorkspaceReader(
                repositories.factory,
                repositories.policy,
                request.app.state.workspace_cache,
            )
        else:
            service = WorkspaceService(
                repositories.factory, repositories.policy, request.app.state.settings
            )
        if workspace_id is None:
            return await getattr(service, method)(actor, repository_id, **kwargs)
        return await getattr(service, method)(
            actor, repository_id, workspace_id, **kwargs
        )
    except SQLAlchemyError:
        raise AppError("service_unavailable", 503, "Service unavailable") from None


@router.get("")
async def listing(
    repository_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> WorkspacePage:
    return await invoke(
        request, "list", actor, repository_id, limit=limit, cursor=cursor
    )


@router.post("", status_code=202, dependencies=[Depends(require_csrf)])
async def prepare(
    repository_id: UUID,
    request: Request,
    selection: WorkspaceSelection,
    actor: Annotated[Actor, Depends(require_actor)],
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> WorkspaceView:
    return await invoke(
        request,
        "request",
        actor,
        repository_id,
        selection=selection,
        key=idempotency_key,
    )


@router.get("/{workspace_id}")
async def detail(
    repository_id: UUID,
    workspace_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
) -> WorkspaceView:
    return await invoke(request, "get", actor, repository_id, workspace_id)


@router.get("/{workspace_id}/tree")
async def tree(
    repository_id: UUID,
    workspace_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    path: str = "",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> TreePage:
    return await invoke(
        request,
        "tree",
        actor,
        repository_id,
        workspace_id,
        path=path,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{workspace_id}/file")
async def file(
    repository_id: UUID,
    workspace_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    path: str,
    start_line: Annotated[int, Query(ge=1)] = 1,
    line_count: Annotated[int, Query(ge=1, le=500)] = 500,
) -> FileView:
    return await invoke(
        request,
        "file",
        actor,
        repository_id,
        workspace_id,
        path=path,
        start_line=start_line,
        line_count=line_count,
    )


@router.get("/{workspace_id}/search")
async def search(
    repository_id: UUID,
    workspace_id: UUID,
    request: Request,
    actor: Annotated[Actor, Depends(require_actor)],
    query: str,
    path: str = "",
) -> SearchView:
    return await invoke(
        request, "search", actor, repository_id, workspace_id, query=query, path=path
    )
