"""Background loop ownership: cadence, outage backoff, crash containment and loop health."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from horizon_chat.api.dependencies import ReadinessProbe
from horizon_chat.ports.errors import DependencyUnavailableError

logger = logging.getLogger(__name__)

# One pass of a loop; True means more work is already due, so the next pass starts at once.
type Iteration = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class ProcessHealth:
    """Mutated only by `run_supervised`: required loops that stopped unexpectedly."""

    stopped: set[str] = field(default_factory=set)

    def loops_alive(self) -> bool:
        return not self.stopped


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopPolicy:
    interval_seconds: float
    max_backoff_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class SupervisedLoop:
    name: str
    iteration: Iteration
    policy: LoopPolicy


async def run_supervised(
    *,
    name: str,
    iteration: Iteration,
    policy: LoopPolicy,
    stop: asyncio.Event,
    health: ProcessHealth,
) -> None:
    """Run `iteration` every interval until `stop`; an outage backs off, anything else ends it.

    A crashed loop is logged once and recorded in `health`, so readiness and liveness report
    the process unhealthy and the platform restarts it; the process keeps serving meanwhile.
    """
    delay = policy.interval_seconds
    degraded = False
    try:
        while not stop.is_set():
            more_due = False
            try:
                more_due = await iteration()
            except DependencyUnavailableError:
                if not degraded:
                    logger.warning("loop_dependency_unavailable", extra={"loop": name})
                degraded = True
                delay = min(delay * 2, policy.max_backoff_seconds)
            else:
                if degraded:
                    logger.info("loop_recovered", extra={"loop": name})
                degraded = False
                delay = policy.interval_seconds
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=0 if more_due else delay)
    except Exception:
        logger.exception("loop_crashed", extra={"loop": name})
        health.stopped.add(name)


@asynccontextmanager
async def supervised_loops(
    loops: Sequence[SupervisedLoop], *, health: ProcessHealth, grace_seconds: float
) -> AsyncIterator[None]:
    """Run every loop for the lifetime of the context.

    On exit the loops get `grace_seconds` to finish their current pass, then are cancelled;
    each pass is resumable, so cancelled work continues on the next start.
    """
    stop = asyncio.Event()
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(
                run_supervised(
                    name=loop.name,
                    iteration=loop.iteration,
                    policy=loop.policy,
                    stop=stop,
                    health=health,
                ),
                name=loop.name,
            )
            for loop in loops
        ]
        try:
            yield
        finally:
            stop.set()
            _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            for task in pending:
                logger.warning("loop_shutdown_cancelled", extra={"loop": task.get_name()})
                task.cancel()


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessReadiness:
    """Ready only while the database is compatible and every required loop is running."""

    database: ReadinessProbe
    health: ProcessHealth

    async def check(self) -> bool:
        return self.health.loops_alive() and await self.database.check()
