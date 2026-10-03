"""Worker loops: outages back off, a crash stops the whole worker for restart."""

import asyncio
import logging

import pytest

from horizon_ingestion.bootstrap.supervisor import LoopPolicy, ProcessHealth, run_supervised
from horizon_ingestion.ports.errors import DependencyUnavailableError

POLICY = LoopPolicy(interval_seconds=0.001, max_backoff_seconds=0.004)


async def test_a_crashed_loop_stops_the_worker() -> None:
    stop, health = asyncio.Event(), ProcessHealth()

    async def crash() -> None:
        raise RuntimeError("bug")

    await asyncio.wait_for(
        run_supervised(
            name="job-dispatch", iteration=crash, policy=POLICY, stop=stop, health=health
        ),
        timeout=1,
    )
    assert stop.is_set()
    assert health.stopped == {"job-dispatch"}


async def test_an_outage_keeps_the_loop_running_until_stop() -> None:
    stop, health = asyncio.Event(), ProcessHealth()
    calls = 0

    async def unavailable() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            stop.set()
        raise DependencyUnavailableError("database_unavailable")

    await asyncio.wait_for(
        run_supervised(
            name="orphan-reconciliation",
            iteration=unavailable,
            policy=POLICY,
            stop=stop,
            health=health,
        ),
        timeout=1,
    )
    assert calls == 3
    assert health.loops_alive()


async def test_a_wakeup_runs_the_next_iteration_early() -> None:
    stop, wakeup, health = asyncio.Event(), asyncio.Event(), ProcessHealth()
    ran = asyncio.Event()
    calls = 0

    async def iteration() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            ran.set()

    loop = asyncio.create_task(
        run_supervised(
            name="job-dispatch",
            iteration=iteration,
            policy=LoopPolicy(interval_seconds=60, max_backoff_seconds=60),
            stop=stop,
            health=health,
            wakeup=wakeup,
        )
    )
    await asyncio.sleep(0.01)
    wakeup.set()
    await asyncio.wait_for(ran.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(loop, timeout=1)


async def test_a_crashed_listener_is_logged_and_dispatch_keeps_scanning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop, health = asyncio.Event(), ProcessHealth()
    scans = 0
    rescanned = asyncio.Event()

    async def listen() -> None:
        raise RuntimeError("listener bug")

    async def claim_due() -> None:
        nonlocal scans
        scans += 1
        if scans == 5:
            rescanned.set()

    with caplog.at_level(logging.ERROR):
        loops = asyncio.gather(
            run_supervised(
                name="job-listener",
                iteration=listen,
                policy=POLICY,
                stop=stop,
                health=ProcessHealth(),
                required=False,
            ),
            run_supervised(
                name="job-dispatch", iteration=claim_due, policy=POLICY, stop=stop, health=health
            ),
        )
        await asyncio.wait_for(rescanned.wait(), timeout=1)
        assert not stop.is_set()
        stop.set()
        await asyncio.wait_for(loops, timeout=1)
    assert health.loops_alive()
    assert [(record.msg, getattr(record, "loop", None)) for record in caplog.records] == [
        ("ingestion_loop_crashed", "job-listener")
    ]
