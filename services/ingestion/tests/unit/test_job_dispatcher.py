"""The dispatcher claims only free capacity and drains claims on shutdown."""

import asyncio
from dataclasses import dataclass, field, replace

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from horizon_ingestion.observability.tracing import Measurements, Telemetry
from horizon_ingestion.ports.indexing import Claim
from horizon_ingestion.workers.jobs import JobDispatcher, WorkerRuntime
from horizon_ingestion_testing.jobs import (
    POLICY,
    VECTOR,
    RecordingIndex,
    RecordingQueue,
    index_claim,
    job_context,
)


@dataclass(slots=True, kw_only=True)
class QueuedClaims(RecordingQueue):
    """Hands out the queued claims once; records each claim limit asked for."""

    queued: list[Claim] = field(default_factory=list)
    limits: list[int] = field(default_factory=list)

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: float) -> tuple[Claim, ...]:
        self.limits.append(limit)
        taken, self.queued = self.queued[:limit], self.queued[limit:]
        return tuple(taken)


@dataclass(slots=True, kw_only=True)
class GatedEmbeddings:
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def embed(self, *, text: str) -> tuple[float, ...]:
        await self.release.wait()
        return VECTOR


def dispatcher(queue: QueuedClaims, embeddings: GatedEmbeddings) -> JobDispatcher:
    traces, meters = TracerProvider(), MeterProvider()
    telemetry = Telemetry(
        tracer=traces.get_tracer("test"), measurements=Measurements(meter=meters.get_meter("test"))
    )
    context = replace(
        job_context(embeddings=embeddings, queue=queue, index=RecordingIndex()),
        # A cancelled claim waits out its in-flight provider call, bounded by the chunk deadline.
        policy=replace(POLICY, concurrency=2, chunk_timeout_seconds=0.05),
    )
    runtime = WorkerRuntime(telemetry=telemetry, job=context, worker_id="test-worker")
    return JobDispatcher(runtime=runtime, wakeup=asyncio.Event())


async def test_only_free_capacity_is_claimed() -> None:
    queue = QueuedClaims(queued=[index_claim(), index_claim(), index_claim()])
    embeddings = GatedEmbeddings()
    jobs = dispatcher(queue, embeddings)
    await jobs.claim_due()
    await jobs.claim_due()
    assert queue.limits == [2]  # no capacity left on the second scan
    embeddings.release.set()
    await jobs.drain(grace_seconds=1)
    await jobs.claim_due()
    assert queue.limits == [2, 2]


async def test_drain_settles_within_grace_and_releases_the_rest() -> None:
    queue = QueuedClaims(queued=[index_claim()])
    jobs = dispatcher(queue, GatedEmbeddings())
    await jobs.claim_due()
    await asyncio.sleep(0)
    await jobs.drain(grace_seconds=0.01)
    assert not jobs.running
    assert queue.releases == [True]
    assert queue.failures == []
