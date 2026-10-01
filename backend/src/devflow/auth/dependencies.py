"""Shared request authentication and browser-write protection."""

from typing import Annotated

from fastapi import Depends, Request

from devflow.auth.service import AuthService, forbidden, unauthenticated
from devflow.auth.types import Actor
from devflow.errors import AppError
from devflow.security import verify_csrf


def session_cookie_name(request: Request) -> str:
    return (
        "__Host-devflow_session"
        if request.app.state.settings.app_env == "production"
        else "devflow_session"
    )


def oauth_cookie_name(request: Request) -> str:
    return (
        "__Host-devflow_oauth"
        if request.app.state.settings.app_env == "production"
        else "devflow_oauth"
    )


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise AppError("service_unavailable", 503, "Service unavailable")
    return service


async def require_actor(request: Request) -> Actor:
    raw = request.cookies.get(session_cookie_name(request))
    if not raw:
        raise unauthenticated()
    service = get_auth_service(request)
    return await service.resolve_session(raw)


async def require_admin(
    request: Request, actor: Annotated[Actor, Depends(require_actor)]
) -> Actor:
    if actor.role != "admin":
        await get_auth_service(request).audit_denial("forbidden", actor)
        raise forbidden()
    return actor


async def require_csrf(
    request: Request, actor: Annotated[Actor, Depends(require_actor)]
) -> Actor:
    service = get_auth_service(request)
    origin = request.app.state.settings.public_base_url.rstrip("/")
    supplied = request.headers.get("X-CSRF-Token", "")
    if request.headers.get("Origin") != origin or not verify_csrf(
        service.signing_key(), request.cookies[session_cookie_name(request)], supplied
    ):
        await service.audit_denial("forbidden", actor)
        raise forbidden()
    return actor
