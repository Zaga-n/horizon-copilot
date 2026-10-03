"""Allowlisted HTTP problem responses never expose exception text or tokens."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.feedback import FeedbackTargetError
from horizon_chat.domain.runs import (
    ConversationBusyError,
    IdempotencyConflictError,
    InvalidTerminalStateError,
    StaleRetryError,
)
from horizon_chat.ports.conversations import ConversationStoreRejectedError
from horizon_chat.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)
from horizon_chat.ports.identity import IdentityUnavailableError, InvalidIdentityError


@dataclass(frozen=True, slots=True, kw_only=True)
class Problem:
    status: int
    code: str


logger = logging.getLogger(__name__)

INTERNAL = Problem(status=status.HTTP_500_INTERNAL_SERVER_ERROR, code="internal_error")

# Keyed by exception type; Starlette resolves handlers along the MRO, so a port error
# without its own entry maps through its classification base.
PROBLEMS: dict[type[Exception], Problem] = {
    InvalidIdentityError: Problem(status=status.HTTP_401_UNAUTHORIZED, code="invalid_identity"),
    IdentityUnavailableError: Problem(
        status=status.HTTP_503_SERVICE_UNAVAILABLE, code="identity_unavailable"
    ),
    ConversationNotFoundError: Problem(
        status=status.HTTP_404_NOT_FOUND, code="conversation_not_found"
    ),
    ConversationBusyError: Problem(status=status.HTTP_409_CONFLICT, code="conversation_busy"),
    IdempotencyConflictError: Problem(status=status.HTTP_409_CONFLICT, code="idempotency_conflict"),
    StaleRetryError: Problem(status=status.HTTP_409_CONFLICT, code="stale_retry"),
    FeedbackTargetError: Problem(
        status=status.HTTP_422_UNPROCESSABLE_CONTENT, code="feedback_target"
    ),
    ConversationStoreRejectedError: Problem(
        status=status.HTTP_422_UNPROCESSABLE_CONTENT, code="invalid_input"
    ),
    RejectedError: Problem(status=status.HTTP_422_UNPROCESSABLE_CONTENT, code="request_rejected"),
    # Integrity faults subclass the rejected base; they are our defect, not the client's.
    DataIntegrityError: INTERNAL,
    InvalidTerminalStateError: INTERNAL,
    DependencyUnavailableError: Problem(
        status=status.HTTP_503_SERVICE_UNAVAILABLE, code="service_unavailable"
    ),
}


def problem_response(problem: Problem, *, retry_after_seconds: float | None = None) -> JSONResponse:
    """The one response envelope; exception text never reaches the client."""
    headers = {"Cache-Control": "no-store"}
    if problem.status == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(ceil(retry_after_seconds))
    return JSONResponse(
        status_code=problem.status,
        media_type="application/problem+json",
        headers=headers,
        content={
            "type": f"urn:horizon:{problem.code}",
            "title": problem.code,
            "status": problem.status,
        },
    )


def _handler(problem: Problem) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handle(_request: Request, exc: Exception) -> Response:
        if problem.status >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.exception("request_failed")
        retry_after = (
            exc.retry_after_seconds if isinstance(exc, DependencyUnavailableError) else None
        )
        return problem_response(problem, retry_after_seconds=retry_after)

    return handle


class UnhandledErrors:
    """Answer an unmapped exception with the internal problem, logged once.

    Starlette's own catch-all handler re-raises to the server, which logs the same failure a
    second time. A response that already started (a stream) cannot be replaced, so that
    failure is left to the server's single record.
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
            await problem_response(INTERNAL)(scope, receive, send)


def register_exception_handlers(app: FastAPI) -> None:
    """Call before adding other middleware, so the fallback sits innermost."""
    for exception, problem in PROBLEMS.items():
        app.add_exception_handler(exception, _handler(problem))
    app.add_middleware(UnhandledErrors)
