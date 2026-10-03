"""Bound bytes before multipart spooling, including requests without Content-Length."""

from dataclasses import dataclass, field

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyTooLargeError(Exception):
    """The actual ASGI request stream exceeds the configured upload envelope."""


@dataclass(frozen=True, slots=True)
class RequestBodyLimit:
    """Never trust a declared Content-Length to enforce the spool limit."""

    app: ASGIApp
    max_bytes: int = field(kw_only=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        size = 0

        async def bounded_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.max_bytes:
                    raise BodyTooLargeError("file_too_large")
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLargeError:
            response = JSONResponse(
                {"detail": "file_too_large"}, status_code=413, headers={"Cache-Control": "no-store"}
            )
            await response(scope, receive, send)
