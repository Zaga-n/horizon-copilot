"""One job attempt settles exactly once: retry, terminal failure, fenced stop or release."""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from uuid import UUID

import pytest

from horizon_ingestion.application.process_job import Failed, Ready, process_job
from horizon_ingestion.domain.documents import ErrorCategory
from horizon_ingestion.ports.indexing import (
    ChunkNotProcessingError,
    Claim,
    EmbeddingRejectedError,
    VersionWork,
)
from horizon_ingestion.workers.jobs import WorkerRuntime, run_claim
from horizon_ingestion_testing.jobs import (
    POLICY,
    VECTOR,
    Failure,
    RecordingIndex,
    RecordingQueue,
    index_claim,
    job_context,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SlowEmbeddings:
    delay_seconds: float

    async def embed(self, *, text: str) -> tuple[float, ...]:
        await asyncio.sleep(self.delay_seconds)
        return VECTOR


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectingEmbeddings:
    async def embed(self, *, text: str) -> tuple[float, ...]:
        raise EmbeddingRejectedError("provider_rejected")


@dataclass(frozen=True, slots=True, kw_only=True)
class FixedEmbeddings:
    async def embed(self, *, text: str) -> tuple[float, ...]:
        return VECTOR


@dataclass(slots=True, kw_only=True)
class HoldingEmbeddings:
    """Mutated by calls held at the provider boundary to expose peak concurrency."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    at_capacity: asyncio.Event = field(default_factory=asyncio.Event)
    active: int = 0
    peak: int = 0
    calls: int = 0

    async def embed(self, *, text: str) -> tuple[float, ...]:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 2:
            self.at_capacity.set()
        try:
            await self.release.wait()
            return VECTOR
        finally:
            self.active -= 1


@dataclass(slots=True, kw_only=True)
class StartedQueue(RecordingQueue):
    """Signals when all competing jobs have started their attempt."""

    all_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def start_attempt(self, *, claim: Claim) -> Claim:
        started = await super(StartedQueue, self).start_attempt(claim=claim)
        if self.started == 3:
            self.all_started.set()
        return started


async def test_shared_semaphore_bounds_embedding_calls_across_indexing_jobs() -> None:
    embeddings = HoldingEmbeddings()
    queue = StartedQueue()
    indexes = [RecordingIndex() for _ in range(3)]
    context = job_context(embeddings=embeddings, queue=queue, index=indexes[0])
    async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
        jobs = [
            tasks.create_task(
                process_job(claim=index_claim(), context=replace(context, index=index))
            )
            for index in indexes
        ]
        await queue.all_started.wait()
        await embeddings.at_capacity.wait()
        assert embeddings.calls == 2
        embeddings.release.set()
    assert embeddings.calls == 3
    assert embeddings.peak == 2
    assert all(isinstance(job.result(), Ready) for job in jobs)
    assert all(index.published for index in indexes)


@dataclass(slots=True, kw_only=True)
class StaleIndex(RecordingIndex):
    async def complete_chunk(
        self, *, claim: Claim, chunk_id: UUID, vector: tuple[float, ...]
    ) -> None:
        raise ChunkNotProcessingError()


@dataclass(slots=True, kw_only=True)
class ContendedQueue(RecordingQueue):
    """Every vendor slot stays busy for the first `busy_scans` attempts."""

    busy_scans: int

    async def acquire_permit(self, *, token: UUID, cap: int, lease_seconds: float) -> int | None:
        if self.busy_scans:
            self.busy_scans -= 1
            return None
        return 1


async def test_slow_provider_beyond_chunk_deadline_schedules_a_retry() -> None:
    queue = RecordingQueue()
    context = job_context(
        embeddings=SlowEmbeddings(delay_seconds=0.3),
        queue=queue,
        index=RecordingIndex(),
        policy=replace(POLICY, chunk_timeout_seconds=0.05),
    )
    await process_job(claim=index_claim(), context=context)
    assert queue.failures == [Failure(category=ErrorCategory.PROVIDER, delay_seconds=0.5)]


async def test_waiting_for_a_vendor_permit_is_not_provider_time() -> None:
    queue = ContendedQueue(busy_scans=3)

    async def scan_interval(delay: float) -> None:
        await asyncio.sleep(0.03)

    index = RecordingIndex()
    context = replace(
        job_context(
            embeddings=FixedEmbeddings(),
            queue=queue,
            index=index,
            policy=replace(POLICY, chunk_timeout_seconds=0.05),
        ),
        sleep=scan_interval,
    )
    await process_job(claim=index_claim(), context=context)
    assert queue.failures == []
    assert index.published


async def test_waiting_for_vendor_permits_does_not_spend_the_job_budget() -> None:
    queue = ContendedQueue(busy_scans=4)

    async def scan_interval(delay: float) -> None:
        await asyncio.sleep(0.03)

    index = RecordingIndex()
    context = replace(
        job_context(
            embeddings=FixedEmbeddings(),
            queue=queue,
            index=index,
            # Four contended scans take about twice the whole job budget.
            policy=replace(POLICY, job_timeout_seconds=0.06, chunk_timeout_seconds=0.05),
        ),
        sleep=scan_interval,
    )
    outcome = await process_job(claim=index_claim(), context=context)
    assert isinstance(outcome, Ready)
    assert queue.failures == []
    assert index.published


async def test_waiting_for_local_embedding_capacity_does_not_spend_the_job_budget() -> None:
    queue = RecordingQueue()
    index = RecordingIndex()
    semaphore = asyncio.Semaphore(0)
    context = replace(
        job_context(
            embeddings=FixedEmbeddings(),
            queue=queue,
            index=index,
            policy=replace(POLICY, job_timeout_seconds=0.01),
        ),
        semaphore=semaphore,
    )
    # Exercise the real asyncio timeout: capacity arrives after the work-only budget.
    release = asyncio.get_running_loop().call_later(0.04, semaphore.release)
    try:
        async with asyncio.timeout(1):
            outcome = await process_job(claim=index_claim(), context=context)
    finally:
        release.cancel()
    assert isinstance(outcome, Ready)
    assert queue.failures == []
    assert index.published


async def test_rejected_input_fails_once_with_one_failure_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = RecordingQueue()
    context = job_context(embeddings=RejectingEmbeddings(), queue=queue, index=RecordingIndex())
    runtime = WorkerRuntime(telemetry=context.telemetry, job=context, worker_id="test-worker")
    with caplog.at_level(logging.INFO):
        await run_claim(claim=index_claim(), runtime=runtime)
    assert queue.failures == [Failure(category=ErrorCategory.REJECTED, delay_seconds=None)]
    assert [record.msg for record in caplog.records].count("ingestion_job_failed") == 1


@dataclass(slots=True, kw_only=True)
class DefectiveIndex(RecordingIndex):
    async def version(self, *, claim: Claim) -> VersionWork:
        raise RuntimeError("bug")


async def test_a_defect_is_settled_once_against_the_started_counters() -> None:
    queue = RecordingQueue()
    context = job_context(embeddings=FixedEmbeddings(), queue=queue, index=DefectiveIndex())
    # The started attempt is the last of the budget, so the defect ends the job.
    claim = index_claim(cycle_attempts=POLICY.max_job_attempts - 1)
    outcome = await process_job(claim=claim, context=context)
    assert isinstance(outcome, Failed)
    assert outcome.claim.cycle_attempts == POLICY.max_job_attempts
    assert isinstance(outcome.failure, RuntimeError)
    assert queue.failures == [Failure(category=ErrorCategory.INTERNAL, delay_seconds=None)]


async def test_a_ready_outcome_carries_the_started_claim() -> None:
    outcome = await process_job(
        claim=index_claim(),
        context=job_context(
            embeddings=FixedEmbeddings(), queue=RecordingQueue(), index=RecordingIndex()
        ),
    )
    assert isinstance(outcome, Ready)
    assert outcome.claim.attempts == 1


async def test_stale_chunk_completion_stops_without_settling_the_job() -> None:
    queue = RecordingQueue()
    context = job_context(embeddings=FixedEmbeddings(), queue=queue, index=StaleIndex())
    await process_job(claim=index_claim(), context=context)
    assert queue.failures == []
    assert queue.releases == []


async def test_cancelled_attempt_is_released_uncounted() -> None:
    queue = RecordingQueue()
    context = job_context(
        embeddings=SlowEmbeddings(delay_seconds=0.3), queue=queue, index=RecordingIndex()
    )
    task = asyncio.create_task(process_job(claim=index_claim(), context=context))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.releases == [True]
    assert queue.failures == []


async def test_attempt_beyond_the_job_budget_fails_without_provider_calls() -> None:
    queue = RecordingQueue()
    index = RecordingIndex()
    context = job_context(embeddings=RejectingEmbeddings(), queue=queue, index=index)
    await process_job(claim=index_claim(cycle_attempts=POLICY.max_job_attempts), context=context)
    assert queue.failures == [Failure(category=ErrorCategory.BUDGET, delay_seconds=None)]
    assert not index.published
