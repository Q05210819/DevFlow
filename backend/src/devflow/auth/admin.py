"""Administrator allowlist and redacted audit views."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from devflow.auth.dependencies import get_auth_service, require_admin, require_csrf
from devflow.auth.schemas import MemberCreate, MemberUpdate, UserView
from devflow.auth.types import Actor
from devflow.pagination import Page

router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin)])
Admin = Annotated[Actor, Depends(require_admin)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/users", response_model=Page[UserView])
async def list_users(
    request: Request, actor: Admin, limit: Limit = 20, cursor: str | None = None
):
    return await get_auth_service(request).list_members(actor, limit, cursor)


@router.post(
    "/users",
    response_model=UserView,
    status_code=201,
    dependencies=[Depends(require_csrf)],
)
async def create_user(request: Request, actor: Admin, body: MemberCreate):
    return await get_auth_service(request).create_member(
        actor, body.github_id, body.role
    )


@router.patch(
    "/users/{user_id}", response_model=UserView, dependencies=[Depends(require_csrf)]
)
async def update_user(
    request: Request, actor: Admin, user_id: UUID, body: MemberUpdate
):
    return await get_auth_service(request).update_member(
        actor, user_id, body.role, body.enabled
    )
