"""Stable public error categories without raw parser, provider or driver exceptions."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil

from fastapi import FastAPI, Request, status
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from horizon_ingestion.api.middleware import BodyTooLargeError
from horizon_ingestion.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)
from horizon_ingestion.ports.identity import IdentityUnavailableError, InvalidIdentityError
from horizon_ingestion.ports.indexing import StatusConflictError, StatusNotFoundError
from horizon_ingestion.ports.uploads import (
    ConflictError,
    FileTooLargeError,
    NotFoundError,
    UploadValidationError,
)

logger = logging.getLogger(__name__)


class UploadFormError(RejectedError):
    """The multipart form lacks the file, or its metadata or document id is malformed."""


class UploadTimeoutError(Exception):
    """The upload request did not finish within its deadline."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicError:
    status: int
    code: str
    # Upload validation codes are authored for the client, so they pass through.
    exposes_error_code: bool = False


INTERNAL = PublicError(status=status.HTTP_500_INTERNAL_SERVER_ERROR, code="internal_error")

# Keyed by exception type; Starlette resolves handlers along the MRO, so a port error
# without its own entry maps through its classification base.
PUBLIC_ERRORS: dict[type[Exception], PublicError] = {
    InvalidIdentityError: PublicError(status=status.HTTP_401_UNAUTHORIZED, code="invalid_identity"),
    IdentityUnavailableError: PublicError(
        status=status.HTTP_503_SERVICE_UNAVAILABLE, code="service_unavailable"
    ),
    NotFoundError: PublicError(status=status.HTTP_404_NOT_FOUND, code="not_found"),
    ConflictError: PublicError(status=status.HTTP_409_CONFLICT, code="request_conflict"),
    StatusNotFoundError: PublicError(status=status.HTTP_404_NOT_FOUND, code="not_found"),
    StatusConflictError: PublicError(status=status.HTTP_409_CONFLICT, code="request_conflict"),
    FileTooLargeError: PublicError(status=status.HTTP_413_CONTENT_TOO_LARGE, code="file_too_large"),
    BodyTooLargeError: PublicError(status=status.HTTP_413_CONTENT_TOO_LARGE, code="file_too_large"),
    UploadValidationError: PublicError(
        status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="invalid_upload",
        exposes_error_code=True,
    ),
    UploadFormError: PublicError(
        status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="invalid_upload",
        exposes_error_code=True,
    ),
    UploadTimeoutError: PublicError(status=status.HTTP_408_REQUEST_TIMEOUT, code="upload_timeout"),
    RejectedError: PublicError(
        status=status.HTTP_422_UNPROCESSABLE_CONTENT, code="request_rejected"
    ),
    # Integrity faults subclass the rejected base; they are our defect, not the client's.
    DataIntegrityError: INTERNAL,
    DependencyUnavailableError: PublicError(
        status=status.HTTP_503_SERVICE_UNAVAILABLE, code="service_unavailable"
    ),
}


def error_response(
    error: PublicError, *, detail: str | None = None, retry_after_seconds: float | None = None
) -> JSONResponse:
    """The one response envelope; exception text never reaches the client."""
    headers = {"Cache-Control": "no-store"}
    if error.status == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(ceil(retry_after_seconds))
    return JSONResponse({"detail": detail or error.code}, status_code=error.status, headers=headers)


def _handler(error: PublicError) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handle(_request: Request, exc: Exception) -> Response:
        if error.status >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.exception("request_failed")
        detail = (
            exc.error_code if error.exposes_error_code and isinstance(exc, RejectedError) else None
        )
        retry_after = (
            exc.retry_after_seconds if isinstance(exc, DependencyUnavailableError) else None
        )
        return error_response(error, detail=detail, retry_after_seconds=retry_after)

    return handle


class UnhandledErrors:
    """Answer an unmapped exception with the internal error, logged once.

    Starlette's own catch-all handler re-raises to the server, which logs the same failure a
    second time. A response that already started cannot be replaced, so that failure is left
    to the server's single record.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking)
        except Exception:
            if started:
                raise
            logger.exception("request_failed")
            await error_response(INTERNAL)(scope, receive, send)


def register_errors(app: FastAPI) -> None:
    """Call before adding other middleware, so the fallback sits innermost."""
    for exception, error in PUBLIC_ERRORS.items():
        app.add_exception_handler(exception, _handler(error))
    app.add_middleware(UnhandledErrors)
