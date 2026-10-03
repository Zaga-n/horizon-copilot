"""Atomic worker health snapshots for the separate container healthcheck process."""

import asyncio
import json
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TypedDict

from horizon_ingestion.bootstrap.supervisor import ProcessHealth

# Private process-local state; no port or infrastructure credentials are needed.
HEALTH_PATH = Path("/tmp/horizon-ingestion-worker-health.json")


class HealthSnapshot(TypedDict):
    healthy: bool
    listener_connected: bool
    recovery_scan_age_seconds: float | None
    sampled_at: float
    max_age_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerHealth:
    """Recovery scans determine health; LISTEN is an independently reported optimization."""

    loops: ProcessHealth
    listener_connected: asyncio.Event
    max_scan_age_seconds: float

    def snapshot(self, *, now: float) -> HealthSnapshot:
        last_scan = self.loops.completed.get("job-dispatch")
        age = now - last_scan if last_scan is not None else None
        return HealthSnapshot(
            healthy=self.loops.loops_alive()
            and age is not None
            and age <= self.max_scan_age_seconds,
            listener_connected=self.listener_connected.is_set(),
            recovery_scan_age_seconds=age,
            sampled_at=now,
            max_age_seconds=self.max_scan_age_seconds,
        )


def write_snapshot(path: Path, snapshot: HealthSnapshot) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot))
    temporary.replace(path)


async def publish_health(
    *, health: WorkerHealth, stop: asyncio.Event, interval_seconds: float
) -> None:
    try:
        while not stop.is_set():
            await asyncio.to_thread(write_snapshot, HEALTH_PATH, health.snapshot(now=monotonic()))
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
    finally:
        await asyncio.to_thread(HEALTH_PATH.unlink, missing_ok=True)


def check_worker_health() -> int:
    """Print bounded worker state; exit nonzero for an absent, stale, or unhealthy snapshot."""
    try:
        snapshot = json.loads(HEALTH_PATH.read_text())
        healthy = snapshot["healthy"] and (
            0 <= monotonic() - snapshot["sampled_at"] <= snapshot["max_age_seconds"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    sys.stdout.write(json.dumps(snapshot) + "\n")
    return 0 if healthy else 1
