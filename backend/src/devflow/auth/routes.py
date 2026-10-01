"""GitHub login redirects and opaque cookie sessions."""

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from devflow.auth.dependencies import (
    get_auth_service,
    oauth_cookie_name,
    require_actor,
    require_csrf,
    session_cookie_name,
)
from devflow.auth.schemas import SessionView
from devflow.auth.types import Actor
from devflow.errors import AppError

router = APIRouter(prefix="/api/v1")


def _cookie_options(request: Request) -> dict:
    return {
        "httponly": True,
        "secure": request.app.state.settings.app_env == "production",
        "samesite": "lax",
        "path": "/",
    }


@router.get("/auth/github/start")
async def github_start(request: Request, return_path: str = "/repositories"):
    start = await get_auth_service(request).begin_login(return_path)
    response = RedirectResponse(
        start.authorize_url, status_code=302, headers={"Cache-Control": "no-store"}
    )
    response.set_cookie(
        oauth_cookie_name(request),
        start.browser_nonce.get_secret_value(),
        max_age=600,
        **_cookie_options(request),
    )
    return response


@router.get("/auth/github/callback")
async def github_callback(
    request: Request,
    code: Annotated[str, Query(min_length=1, max_length=2048)] = "",
    state: Annotated[str, Query(min_length=1, max_length=512)] = "",
):
    if not code or not state:
        raise AppError("validation_error", 400, "Invalid login callback")
    issue = await get_auth_service(request).complete_login(
        code, state, request.cookies.get(oauth_cookie_name(request), "")
    )
    response = RedirectResponse(
        issue.return_path, status_code=303, headers={"Cache-Control": "no-store"}
    )
    response.set_cookie(
        session_cookie_name(request),
        issue.raw_token.get_secret_value(),
        max_age=28800,
        expires=issue.expires_at.astimezone(UTC),
        **_cookie_options(request),
    )
    response.delete_cookie(oauth_cookie_name(request), **_cookie_options(request))
    return response


@router.get("/session", response_model=SessionView)
async def current_session(
    request: Request,
    response: Response,
    actor: Annotated[Actor, Depends(require_actor)],
):
    service = get_auth_service(request)
    response.headers["Cache-Control"] = "no-store"
    return SessionView(
        user=await service.user_view(actor),
        csrf_token=service.session_csrf(request.cookies[session_cookie_name(request)]),
    )


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, actor: Annotated[Actor, Depends(require_csrf)]):
    await get_auth_service(request).logout(
        request.cookies[session_cookie_name(request)]
    )
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(session_cookie_name(request), **_cookie_options(request))
    return response
