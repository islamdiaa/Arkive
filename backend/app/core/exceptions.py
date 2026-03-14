"""Custom exception hierarchy and FastAPI exception handlers."""

import json
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Map HTTP status codes to error code strings
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "target_error",
    503: "service_unavailable",
}


class ArkiveError(Exception):
    """Base exception for Arkive."""

    status_code: int = 500

    def __init__(self, message: str = "Internal server error"):
        self.message = message
        super().__init__(message)


class NotFoundError(ArkiveError):
    """404 Not Found."""

    status_code = 404

    def __init__(self, message: str = "Resource not found"):
        super().__init__(message)


class AuthError(ArkiveError):
    """401 Unauthorized."""

    status_code = 401

    def __init__(self, message: str = "Authentication required"):
        super().__init__(message)


class ValidationError(ArkiveError):
    """422 Unprocessable Entity."""

    status_code = 422

    def __init__(self, message: str = "Validation error"):
        super().__init__(message)


class RateLimitError(ArkiveError):
    """429 Too Many Requests."""

    status_code = 429

    def __init__(self, message: str = "Too many failed attempts. Try again later.", retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(message)


class TargetError(ArkiveError):
    """Target connection/upload error."""

    status_code = 502

    def __init__(self, message: str = "Storage target error"):
        super().__init__(message)


class BackupError(ArkiveError):
    """Backup pipeline error — 409 when backup already in progress."""

    status_code = 409

    def __init__(self, message: str = "Backup error", run_id: str | None = None):
        self.run_id = run_id
        super().__init__(message)


def _json_safe(value: Any) -> Any:
    """Convert exception payloads into JSON-serializable values."""
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def register_exception_handlers(app) -> None:
    """Register all custom exception handlers for consistent error shape."""

    @app.exception_handler(ArkiveError)
    async def arkive_error_handler(request: Request, exc: ArkiveError):
        code = _STATUS_CODES.get(exc.status_code, "error")
        headers = {}
        details: dict = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after)
            details["retry_after"] = exc.retry_after
        if isinstance(exc, BackupError) and exc.run_id:
            details["run_id"] = exc.run_id
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": exc.message, "details": details},
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        code = _STATUS_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": code, "message": message, "details": {}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = [_json_safe(error) for error in exc.errors()]
        message = "; ".join(f"{e.get('loc', ['?'])[-1]}: {e.get('msg', '?')}" for e in errors)
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "message": message, "details": {"errors": errors}},
        )

    @app.exception_handler(json.JSONDecodeError)
    async def json_decode_handler(request, exc):
        return JSONResponse(
            status_code=422, content={"error": "validation_error", "message": "Invalid JSON body", "details": {}}
        )
