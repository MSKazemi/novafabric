"""Error types and HTTP error envelope for the NovaFabric REST API.

All error responses follow the ADR-0017 envelope:
    {"error": {"code": "<string>", "message": "<string>", "details": {...}}}
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build a standard error envelope JSON response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


# ---------- named exception classes ----------


class NotFoundError(Exception):
    """Resource not found."""

    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message)
        self.code = code


class ConflictError(Exception):
    """Resource conflict (duplicate, invalid lifecycle transition)."""

    def __init__(
        self,
        message: str,
        code: str = "conflict",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class ValidationError(Exception):
    """Spec or request body validation failure."""

    def __init__(
        self,
        message: str,
        code: str = "validation_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class PayloadTooLargeError(Exception):
    """Upload exceeds ``server.ingest.max_upload_bytes`` (ADR-0203, 413)."""

    def __init__(
        self,
        message: str,
        code: str = "payload_too_large",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class PreconditionFailedError(Exception):
    """Promotion gate blocked (agent missing passing eval)."""

    def __init__(self, message: str, code: str = "precondition_failed") -> None:
        super().__init__(message)
        self.code = code


class BadRequestError(Exception):
    """Invalid request parameters."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


# ---------- FastAPI exception handlers ----------


async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return error_response(404, exc.code, str(exc))


async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
    return error_response(409, exc.code, str(exc), exc.details)


async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return error_response(422, exc.code, str(exc), exc.details)


async def payload_too_large_handler(
    request: Request, exc: PayloadTooLargeError
) -> JSONResponse:
    # No Retry-After header — size does not decay on a clock (spec).
    return error_response(413, exc.code, str(exc), exc.details)


async def precondition_failed_handler(
    request: Request, exc: PreconditionFailedError
) -> JSONResponse:
    return error_response(412, exc.code, str(exc))


async def bad_request_handler(request: Request, exc: BadRequestError) -> JSONResponse:
    return error_response(400, exc.code, str(exc))


def register_handlers(app: Any) -> None:
    """Attach all custom exception handlers to a FastAPI app instance."""
    app.add_exception_handler(NotFoundError, not_found_handler)
    app.add_exception_handler(ConflictError, conflict_handler)
    app.add_exception_handler(ValidationError, validation_error_handler)
    app.add_exception_handler(PayloadTooLargeError, payload_too_large_handler)
    app.add_exception_handler(PreconditionFailedError, precondition_failed_handler)
    app.add_exception_handler(BadRequestError, bad_request_handler)
