"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from devflow.audit.routes import router as audit_router
from devflow.auth.admin import router as admin_router
from devflow.auth.routes import router as auth_router
from devflow.auth.service import AuthService
from devflow.config import Settings
from devflow.db import create_session_factory, schema_is_current
from devflow.errors import AppError
from devflow.github.client import GitHubClient
from devflow.github.credentials import CredentialService
from devflow.logging import configure_logging, log_request
from devflow.security import CredentialCipher


def _error_response(
    request: Request, code: str, status: int, message: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.state.request_id,
            }
        },
    )


def create_app(
    settings: Settings | None = None, *, github: GitHubClient | None = None
) -> FastAPI:
    configure_logging()
    configured = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        factory = (
            create_session_factory(configured) if configured.database_url else None
        )
        app.state.session_factory = factory
        http = None
        try:
            cipher = (
                CredentialCipher.from_file(configured.credential_encryption_key_file)
                if configured.credential_encryption_key_file is not None
                else None
            )
            if github is None:
                http = httpx.AsyncClient()
                app.state.github = GitHubClient(http, configured)
            else:
                app.state.github = github
            app.state.cipher = cipher
            app.state.credential_service = (
                CredentialService(factory, app.state.github, cipher)
                if factory is not None and cipher is not None
                else None
            )
            app.state.auth_service = (
                AuthService(factory, app.state.github, cipher, configured)
                if factory is not None
                else None
            )
            yield
        finally:
            try:
                if http is not None:
                    await http.aclose()
            finally:
                if factory is not None:
                    await factory.kw["bind"].dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = configured
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(audit_router)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid4().hex
        try:
            response = await call_next(request)
        # Keep raw exception details out of the ASGI server's error log.
        except Exception:  # noqa: BLE001
            response = _error_response(
                request, "internal_error", 500, "Internal server error"
            )
        response.headers["X-Request-ID"] = request.state.request_id
        log_request(request.state.request_id, response.status_code)
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError):
        return _error_response(request, error.code, error.status, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError):
        return _error_response(request, "validation_error", 422, "Invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        mapping = {
            401: ("unauthenticated", "Authentication required"),
            403: ("forbidden", "Forbidden"),
            404: ("resource_not_found", "Resource not found"),
        }
        code, message = mapping.get(
            error.status_code, ("request_error", "Request failed")
        )
        return _error_response(request, code, error.status_code, message)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _error: Exception):
        return _error_response(request, "internal_error", 500, "Internal server error")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            async with asyncio.timeout(5):
                factory = app.state.session_factory
                if factory is None:
                    raise ValueError("Database is not configured")
                async with factory() as session:
                    if not await schema_is_current(session):
                        raise ValueError("Schema is not current")
        except Exception:  # noqa: BLE001
            raise AppError("service_unavailable", 503, "Service unavailable") from None
        return {"status": "ok"}

    return app
