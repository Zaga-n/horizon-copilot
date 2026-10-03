"""Liveness and bounded readiness probes without exposing dependency exceptions."""

import asyncio

from fastapi import APIRouter, Response, status

from horizon_ingestion.api.dependencies import RuntimeDep

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready")
async def ready(runtime: RuntimeDep, response: Response) -> dict[str, str]:
    try:
        async with asyncio.timeout(runtime.readiness_timeout_seconds):
            available = await runtime.check_readiness()
    except TimeoutError:
        available = False
    response.status_code = status.HTTP_200_OK if available else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if available else "unready"}
