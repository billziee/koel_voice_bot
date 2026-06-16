"""Structured error bodies and HTTP status discipline.

PRD §08 HTTP response codes:
  200/201/204 success · 400 client error (structured body) · 422 validation ·
  500 reserved for *unhandled* exceptions only.

Every error response shares one envelope so the frontend can render failures
uniformly and logs/traces stay greppable by ``error.code``.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from koel.common.logging import get_correlation_id, get_logger

logger = get_logger("koel.errors")


class ErrorBody(BaseModel):
    """Canonical error envelope returned by every service."""

    code: str
    message: str
    detail: object | None = None
    correlation_id: str | None = None


class ApiError(Exception):
    """Raise for expected, client-facing failures (4xx)."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        detail: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def _envelope(status_code: int, code: str, message: str, detail: object | None = None) -> JSONResponse:
    body = ErrorBody(
        code=code,
        message=message,
        detail=detail,
        correlation_id=get_correlation_id(),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        # Expected failures are WARN, not ERROR — they are part of normal flow.
        logger.warning("api_error code=%s status=%s msg=%s", exc.code, exc.status_code, exc.message)
        return _envelope(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Request failed schema validation.",
            detail=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 500 is reserved for genuinely unhandled exceptions (PRD §08).
        logger.error("unhandled_exception: %s", exc, exc_info=True)
        return _envelope(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )
