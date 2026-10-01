"""Administrator access to paged, fixed-field audit records."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from devflow.auth.dependencies import get_auth_service, require_admin
from devflow.auth.schemas import AuditView
from devflow.auth.types import Actor
from devflow.pagination import Page

router = APIRouter(prefix="/api/v1/admin")


@router.get("/audit-logs", response_model=Page[AuditView])
async def audit_logs(
    request: Request,
    actor: Annotated[Actor, Depends(require_admin)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    return await get_auth_service(request).list_audit(actor, limit, cursor)
