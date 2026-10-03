"""Worker health requires recent successful recovery scans, independently of LISTEN."""

import asyncio
from pathlib import Path

from horizon_ingestion.bootstrap.supervisor import ProcessHealth
from horizon_ingestion.bootstrap.worker_health import WorkerHealth, write_snapshot


def test_recent_recovery_scan_keeps_worker_healthy_without_listener() -> None:
    health = WorkerHealth(
        loops=ProcessHealth(completed={"job-dispatch": 10}),
        listener_connected=asyncio.Event(),
        max_scan_age_seconds=20,
    )
    snapshot = health.snapshot(now=15)
    assert snapshot["healthy"]
    assert snapshot["recovery_scan_age_seconds"] == 5
    assert not snapshot["listener_connected"]


def test_stalled_recovery_scan_is_unhealthy_even_with_listener() -> None:
    connected = asyncio.Event()
    connected.set()
    health = WorkerHealth(
        loops=ProcessHealth(completed={"job-dispatch": 10}),
        listener_connected=connected,
        max_scan_age_seconds=20,
    )
    snapshot = health.snapshot(now=31)
    assert not snapshot["healthy"]
    assert snapshot["listener_connected"]


def test_required_loop_crash_is_unhealthy_despite_recent_scan() -> None:
    health = WorkerHealth(
        loops=ProcessHealth(stopped={"orphan-reconciliation"}, completed={"job-dispatch": 10}),
        listener_connected=asyncio.Event(),
        max_scan_age_seconds=20,
    )
    assert not health.snapshot(now=11)["healthy"]


def test_initial_worker_does_not_report_ready_before_first_scan(tmp_path: Path) -> None:
    health = WorkerHealth(
        loops=ProcessHealth(), listener_connected=asyncio.Event(), max_scan_age_seconds=20
    )
    snapshot = health.snapshot(now=10)
    assert not snapshot["healthy"]
    assert snapshot["recovery_scan_age_seconds"] is None
    path = tmp_path / "health.json"
    write_snapshot(path, snapshot)
    assert '"healthy": false' in path.read_text()
    assert not path.with_suffix(".tmp").exists()
