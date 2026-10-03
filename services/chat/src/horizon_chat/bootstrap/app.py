"""ASGI composition root with a single replaceable resource-construction seam."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from horizon_chat.api.dependencies import ApiRuntime
from horizon_chat.api.exception_handlers import register_exception_handlers
from horizon_chat.api.middleware import RequestMetrics
from horizon_chat.api.routers import conversations, health, turns
from horizon_chat.bootstrap.runtime import build_runtime
from horizon_chat.bootstrap.supervisor import ProcessHealth
from horizon_chat.config.secrets import Secrets, load_secrets
from horizon_chat.config.settings import Settings, load_settings

type RuntimeBuilder = Callable[
    [Settings, Secrets, ProcessHealth], AbstractAsyncContextManager[ApiRuntime]
]


def create_app(
    *,
    settings: Settings | None = None,
    runtime_builder: RuntimeBuilder = build_runtime,
) -> FastAPI:
    resolved_settings = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        secrets = load_secrets()
        # The lifespan owns loop health: the runtime's loops record crashes in it, and both
        # liveness and readiness read it.
        health = ProcessHealth()
        app.state.liveness = health
        async with runtime_builder(resolved_settings, secrets, health) as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(title="Horizon chat", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(conversations.router)
    app.include_router(turns.router)
    register_exception_handlers(app)
    app.add_middleware(RequestMetrics)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved_settings.frontend_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        expose_headers=["Content-Type"],
    )
    return app
