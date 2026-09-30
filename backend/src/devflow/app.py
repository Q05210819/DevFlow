"""FastAPI application factory."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from devflow.config import Settings
from devflow.errors import AppError
from devflow.logging import configure_logging, log_request


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


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI()
    app.state.settings = settings if settings is not None else Settings()

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
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

    return app
