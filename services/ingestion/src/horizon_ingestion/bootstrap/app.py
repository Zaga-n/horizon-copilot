"""Ingestion ASGI factory; only lifespan constructs runtime dependencies."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from horizon_ingestion.api.dependencies import ApiRuntime
from horizon_ingestion.api.errors import register_errors
from horizon_ingestion.api.middleware import RequestBodyLimit
from horizon_ingestion.api.routers import documents, health
from horizon_ingestion.bootstrap.runtime import build_runtime
from horizon_ingestion.config.secrets import Secrets, load_secrets
from horizon_ingestion.config.settings import Settings, load_settings

type RuntimeBuilder = Callable[[Settings, Secrets], AbstractAsyncContextManager[ApiRuntime]]


def create_app(
    *, settings: Settings | None = None, runtime_builder: RuntimeBuilder = build_runtime
) -> FastAPI:
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with runtime_builder(resolved, load_secrets()) as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(title="Horizon document ingestion", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(documents.router)
    register_errors(app)
    app.add_middleware(RequestBodyLimit, max_bytes=resolved.max_upload_bytes + 65536)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved.frontend_origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    return app
