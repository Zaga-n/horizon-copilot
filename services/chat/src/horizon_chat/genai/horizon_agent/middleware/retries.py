"""Utility calls use the same finite transient-only policy as the agent loop."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from horizon_chat.genai.horizon_agent.schemas import attempt_context


def transient_model_error(exc: Exception) -> bool:
    if attempt_context.get().final_started:
        return False
    if isinstance(
        exc, (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError, TimeoutError)
    ):
        return True
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "InternalServerException",
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class UtilityRetryPolicy:
    attempts: int
    initial_seconds: float
    max_seconds: float


async def retry_utility[T](*, call: Callable[[], Awaitable[T]], policy: UtilityRetryPolicy) -> T:
    for attempt in range(policy.attempts):
        try:
            return await call()
        except Exception as exc:
            if attempt + 1 >= policy.attempts or not transient_model_error(exc):
                raise
            await asyncio.sleep(min(policy.initial_seconds * 2**attempt, policy.max_seconds))
    raise ValueError("retry policy requires a positive attempt count")
