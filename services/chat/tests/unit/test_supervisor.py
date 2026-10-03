"""Supervised loops: outages back off, crashes stop the loop and make readiness fail."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import pytest

from horizon_chat.bootstrap.runtime import maintenance_loops
from horizon_chat.bootstrap.supervisor import (
    LoopPolicy,
    ProcessHealth,
    ProcessReadiness,
    SupervisedLoop,
    run_supervised,
    supervised_loops,
)
from horizon_chat.config.settings import Settings
from horizon_chat.ports.conversations import ConversationStoreUnavailableError

POLICY = LoopPolicy(interval_seconds=0.001, max_backoff_seconds=0.004)


@dataclass(slots=True, kw_only=True)
class ScriptedIteration:
    """Raises each scripted error in turn, then succeeds; mutated as it runs."""

    errors: list[Exception]
    calls: int = 0
    succeeded: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self) -> bool:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.succeeded.set()
        return False


async def test_a_crashing_loop_is_recorded_and_stops(caplog: pytest.LogCaptureFixture) -> None:
    health = ProcessHealth()
    iteration = ScriptedIteration(errors=[RuntimeError("bug")])
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(
            run_supervised(
                name="conversation-retention",
                iteration=iteration,
                policy=POLICY,
                stop=asyncio.Event(),
                health=health,
            ),
            timeout=1,
        )
    assert not health.loops_alive()
    assert health.stopped == {"conversation-retention"}
    assert iteration.calls == 1
    assert [record.msg for record in caplog.records] == ["loop_crashed"]


async def test_an_outage_backs_off_and_the_loop_keeps_running() -> None:
    health = ProcessHealth()
    stop = asyncio.Event()
    iteration = ScriptedIteration(
        errors=[ConversationStoreUnavailableError("database_unavailable")] * 3
    )
    loop = asyncio.create_task(
        run_supervised(
            name="failure-reconciliation",
            iteration=iteration,
            policy=POLICY,
            stop=stop,
            health=health,
        )
    )
    await asyncio.wait_for(iteration.succeeded.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(loop, timeout=1)
    assert health.loops_alive()
    assert iteration.calls == 4


class CompatibleDatabase:
    async def check(self) -> bool:
        return True


async def test_readiness_fails_while_a_required_loop_is_stopped() -> None:
    health = ProcessHealth()
    readiness = ProcessReadiness(database=CompatibleDatabase(), health=health)
    assert await readiness.check()
    health.stopped.add("conversation-retention")
    assert not await readiness.check()


@dataclass(slots=True, kw_only=True)
class Backlog:
    """Reports more work due until `batches` passes ran; mutated as it runs."""

    batches: int
    calls: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self) -> bool:
        self.calls += 1
        if self.calls >= self.batches:
            self.drained.set()
        return self.calls < self.batches


async def test_due_work_runs_without_waiting_for_the_interval() -> None:
    stop = asyncio.Event()
    backlog = Backlog(batches=3)
    loop = asyncio.create_task(
        run_supervised(
            name="conversation-retention",
            iteration=backlog,
            policy=LoopPolicy(interval_seconds=60, max_backoff_seconds=60),
            stop=stop,
            health=ProcessHealth(),
        )
    )
    await asyncio.wait_for(backlog.drained.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(loop, timeout=1)
    assert backlog.calls == 3


@dataclass(slots=True, kw_only=True)
class SlowPass:
    """A pass that outlives any grace period; records that it started and was cancelled."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    async def __call__(self) -> bool:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return False


async def test_shutdown_cancels_a_slow_pass_after_the_grace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    health = ProcessHealth()
    slow = SlowPass()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        async with supervised_loops(
            [SupervisedLoop(name="conversation-retention", iteration=slow, policy=POLICY)],
            health=health,
            grace_seconds=0.05,
        ):
            await asyncio.wait_for(slow.started.wait(), timeout=1)
    assert time.monotonic() - started < 1
    assert slow.cancelled
    assert health.loops_alive()
    assert [record.msg for record in caplog.records] == ["loop_shutdown_cancelled"]


async def test_shutdown_waits_for_a_pass_that_finishes_within_the_grace() -> None:
    started, finished = asyncio.Event(), asyncio.Event()

    async def quick() -> bool:
        started.set()
        await asyncio.sleep(0.01)
        finished.set()
        return False

    async with supervised_loops(
        [SupervisedLoop(name="failure-reconciliation", iteration=quick, policy=POLICY)],
        health=ProcessHealth(),
        grace_seconds=1,
    ):
        await asyncio.wait_for(started.wait(), timeout=1)
    assert finished.is_set()


async def no_work() -> bool:
    return False


def test_maintenance_loops_take_their_cadence_from_settings(chat_settings: Settings) -> None:
    settings = chat_settings.model_copy(
        update={"recovery_interval_seconds": 7.0, "maintenance_interval_seconds": 60.0}
    )
    loops = maintenance_loops(settings=settings, recovery=no_work, retention=no_work)
    assert [(loop.name, loop.policy) for loop in loops] == [
        (
            "failure-reconciliation",
            LoopPolicy(interval_seconds=7.0, max_backoff_seconds=60.0),
        ),
        (
            "conversation-retention",
            LoopPolicy(interval_seconds=60.0, max_backoff_seconds=60.0),
        ),
    ]
