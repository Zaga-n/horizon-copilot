"""Durable claims, generation fencing, global vendor permits and notification-free scans."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from horizon_ingestion.db.queue import JobListener, PgWorkQueue
from horizon_ingestion.domain.documents import ErrorCategory
from horizon_ingestion.ports.indexing import (
    StaleClaimError,
)
from horizon_ingestion.workers.jobs import WorkerRuntime
from horizon_ingestion_testing.pipeline import Harness, serve, upload_pdf

pytestmark = pytest.mark.integration


async def test_recovery_claims_expired_work_and_fences_previous_generation(
    harness: Harness,
) -> None:
    await upload_pdf(harness, key="restart", text_value="Restart evidence")
    old = await harness.claim()
    assert await harness.queue.claim(worker_id="second-worker", limit=1, lease_seconds=90) == ()
    await harness.queue.heartbeat(claim=old, lease_seconds=90)
    async with harness.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.ingestion_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": old.job_id},
        )
    fresh = await harness.claim()
    assert fresh.generation == old.generation + 1
    with pytest.raises(StaleClaimError):
        await harness.queue.heartbeat(claim=old, lease_seconds=90)
    with pytest.raises(StaleClaimError):
        await harness.index.publish(claim=old)
    await harness.queue.fail(claim=fresh, category=ErrorCategory.PROVIDER, delay_seconds=10)
    assert await harness.queue.claim(worker_id="second-worker", limit=1, lease_seconds=90) == ()
    async with harness.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.ingestion_jobs SET next_retry_at=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": old.job_id},
        )
    resumed = await harness.claim()
    await harness.process(resumed)
    assert (await harness.status.job_status(subject="owner", job_id=old.job_id)).status == "ready"
    ready_retry = await harness.client.post(f"/v1/ingestion-jobs/{old.job_id}:retry")
    assert ready_retry.status_code == 200
    assert await harness.queue.claim(worker_id="second-worker", limit=1, lease_seconds=90) == ()


async def test_global_permits_and_stale_release_across_worker_pools(harness: Harness) -> None:
    other = PgWorkQueue(engine=harness.engine)
    tokens = [uuid4() for _ in range(5)]
    slots = await asyncio.gather(
        *(
            queue.acquire_permit(token=token, cap=2, lease_seconds=45)
            for queue, token in zip(
                (harness.queue, other, harness.queue, other), tokens[:4], strict=True
            )
        )
    )
    assert sum(slot is not None for slot in slots) == 2
    assert await other.acquire_permit(token=tokens[4], cap=2, lease_seconds=45) is None
    winner = next(i for i, slot in enumerate(slots) if slot is not None)
    slot = slots[winner]
    assert slot is not None
    async with harness.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.vendor_permits SET lease_until=now()-interval '1 second' WHERE slot=:slot"
            ),
            {"slot": slot},
        )
    replacement_token = uuid4()
    assert await other.acquire_permit(token=replacement_token, cap=2, lease_seconds=45) == slot
    await harness.queue.release_permit(slot=slot, token=tokens[winner])
    assert await harness.queue.acquire_permit(token=uuid4(), cap=2, lease_seconds=45) is None
    await other.release_permit(slot=slot, token=replacement_token)
    assert await harness.queue.acquire_permit(token=uuid4(), cap=2, lease_seconds=45) == slot


async def test_listener_reconnect_requests_scan_for_missed_notifications(
    harness: Harness, migrated_database: str
) -> None:
    await upload_pdf(harness, key="missed-notify", text_value="Already queued")
    wakeup, stop = asyncio.Event(), asyncio.Event()
    listener = JobListener(
        dsn=migrated_database,
        connect_timeout_seconds=1,
        scan_interval_seconds=0.03,
        reconnect_max_seconds=0.1,
    )
    task = asyncio.create_task(listener.listen(wakeup=wakeup, stop=stop))
    try:
        await asyncio.wait_for(wakeup.wait(), timeout=3)
        wakeup.clear()
        async with harness.engine.begin() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND query='LISTEN ingestion_jobs'"
                )
            )
        await asyncio.wait_for(wakeup.wait(), timeout=3)
        assert (
            len(await harness.queue.claim(worker_id="reconnected", limit=1, lease_seconds=90)) == 1
        )
        wakeup.clear()
        async with harness.engine.begin() as conn:
            await conn.execute(text("NOTIFY ingestion_jobs"))
        await asyncio.wait_for(wakeup.wait(), timeout=3)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)


async def test_dispatch_scans_queued_work_without_notification(harness: Harness) -> None:
    await upload_pdf(harness, key="no-listener", text_value="Durable queued evidence")
    wakeup, stop = asyncio.Event(), asyncio.Event()
    runtime = WorkerRuntime(
        telemetry=harness.telemetry,
        job=replace(
            harness.context(),
            policy=replace(
                harness.policy, scan_interval_seconds=0.01, heartbeat_interval_seconds=0.02
            ),
        ),
        worker_id="always-on-test",
    )
    task = asyncio.create_task(serve(harness, runtime=runtime, wakeup=wakeup, stop=stop))
    try:
        async with asyncio.timeout(5):
            while True:
                jobs = await harness.status.list_documents(subject="owner", limit=10, offset=0)
                if jobs[0].status == "ready":
                    break
                await asyncio.sleep(0.01)
        assert not task.done()
        assert len(harness.embeddings.texts) == 1
    finally:
        stop.set()
        wakeup.set()
        await asyncio.wait_for(task, timeout=3)
