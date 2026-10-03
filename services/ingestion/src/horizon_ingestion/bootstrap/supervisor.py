"""Worker loop ownership: cadence, wakeups, outage backoff and crash handling."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic

from horizon_ingestion.ports.errors import DependencyUnavailableError

logger = logging.getLogger(__name__)

type Iteration = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class ProcessHealth:
    """Mutated only by `run_supervised`: required loops that stopped unexpectedly."""

    stopped: set[str] = field(default_factory=set)
    completed: dict[str, float] = field(default_factory=dict)

    def loops_alive(self) -> bool:
        return not self.stopped


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopPolicy:
    interval_seconds: float
    max_backoff_seconds: float


async def _pause(*, stop: asyncio.Event, wakeup: asyncio.Event | None, seconds: float) -> None:
    """Sleep until `seconds` pass, `stop` is set, or `wakeup` asks for an early iteration."""
    waits = {asyncio.create_task(stop.wait())}
    if wakeup is not None:
        waits.add(asyncio.create_task(wakeup.wait()))
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


async def run_supervised(
    *,
    name: str,
    iteration: Iteration,
    policy: LoopPolicy,
    stop: asyncio.Event,
    health: ProcessHealth,
    wakeup: asyncio.Event | None = None,
    required: bool = True,
) -> None:
    """Run `iteration` every interval (or on `wakeup`) until `stop`; an outage backs off.

    A required loop's crash is logged once, recorded in `health`, and stops the whole worker;
    the orchestrator restarts the process. An optional loop (the notification listener, whose
    work periodic scans also cover) is logged and left stopped while the worker keeps running.
    """
    delay = policy.interval_seconds
    degraded = False
    try:
        while not stop.is_set():
            try:
                await iteration()
            except DependencyUnavailableError:
                if not degraded:
                    logger.warning("ingestion_loop_unavailable", extra={"loop": name})
                degraded = True
                delay = min(delay * 2, policy.max_backoff_seconds)
            else:
                health.completed[name] = monotonic()
                if degraded:
                    logger.info("ingestion_loop_recovered", extra={"loop": name})
                degraded = False
                delay = policy.interval_seconds
            # An outage waits out its backoff; wakeups only shorten a healthy cadence.
            await _pause(stop=stop, wakeup=None if degraded else wakeup, seconds=delay)
    except Exception:
        logger.exception("ingestion_loop_crashed", extra={"loop": name, "required": required})
        if required:
            health.stopped.add(name)
            stop.set()
