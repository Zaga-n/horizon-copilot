"""Technical process probes through a narrow runtime view."""

import asyncio

from fastapi import APIRouter, Response, status

from horizon_chat.api.dependencies import LivenessDep, RuntimeDep

router = APIRouter()


@router.get("/health")
async def health(liveness: LivenessDep, response: Response) -> dict[str, str]:
    """Fails only when a required loop crashed, so the platform restarts the process.

    Dependency outages never fail liveness: loops back off and the readiness probe reports them.
    """
    alive = liveness.loops_alive()
    response.status_code = status.HTTP_200_OK if alive else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "alive" if alive else "stopped"}


@router.get("/ready")
async def ready(runtime: RuntimeDep, response: Response) -> dict[str, str]:
    try:
        async with asyncio.timeout(runtime.readiness_timeout_seconds):
            available = await runtime.readiness.check()
    except TimeoutError:
        available = False
    response.status_code = status.HTTP_200_OK if available else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if available else "unready"}
