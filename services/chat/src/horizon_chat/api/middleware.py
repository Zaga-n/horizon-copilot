"""Request latency includes SSE delivery; labels exclude URLs and identifiers."""

from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from horizon_chat.observability.tracing import Telemetry

METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"})


class RequestMetrics:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = perf_counter()
        response_status = 500

        async def capture(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            runtime: object = getattr(scope["app"].state, "runtime", None)
            telemetry: object = getattr(runtime, "telemetry", None)
            if isinstance(telemetry, Telemetry):
                method = scope["method"] if scope["method"] in METHODS else "OTHER"
                labels = {
                    "app.boundary": "http",
                    "http.request.method": method,
                    "http.response.status_class": f"{response_status // 100}xx",
                    "outcome": "error" if response_status >= 500 else "ok",
                }
                telemetry.measurements.duration.record(perf_counter() - started, labels)
                if response_status >= 500:
                    telemetry.measurements.errors.add(1, labels)
