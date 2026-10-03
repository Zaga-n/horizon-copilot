"""Attempt accounting across shutdowns, drain, poison rows and failure classification."""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from uuid import UUID

import pytest
from sqlalchemy import text

from horizon_ingestion.application.index_version import JobDeadline, index_version
from horizon_ingestion.application.process_job import process_job
from horizon_ingestion.db.indexing import PgIndexStore
from horizon_ingestion.db.transactions import transaction
from horizon_ingestion.domain.documents import ErrorCategory, JobState
from horizon_ingestion.ports.errors import DataIntegrityError, DependencyUnavailableError
from horizon_ingestion.ports.indexing import Claim, EmbeddingUnavailableError
from horizon_ingestion.workers.jobs import WorkerRuntime
from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.jobs import VECTOR
from horizon_ingestion_testing.pipeline import Harness, serve, upload_pdf

pytestmark = pytest.mark.integration
SETTLE_ELSEWHERE = text(
    "UPDATE app.document_chunks SET status='completed', embedding=:vector WHERE version_id=:version"
)


@dataclass(slots=True, kw_only=True)
class GatedEmbeddings:
    """Hold each provider call until the test releases it; mutated by the test."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def embed(self, *, text: str) -> tuple[float, ...]:
        self.entered.set()
        await self.release.wait()
        return VECTOR


async def job_row(harness: Harness, job_id: UUID) -> dict[str, object]:
    async with harness.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status, error_category, attempts, cycle_attempts"
                        " FROM app.ingestion_jobs WHERE id=:id"
                    ),
                    {"id": job_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


async def test_constraint_violation_is_an_integrity_error_not_an_outage(harness: Harness) -> None:
    with pytest.raises(DataIntegrityError):
        async with transaction(harness.engine) as conn:
            await conn.execute(
                text(
                    "INSERT INTO app.ingestion_jobs (id, document_id, kind)"
                    " VALUES (gen_random_uuid(), gen_random_uuid(), 'index')"
                )
            )


async def test_three_shutdowns_before_settling_leave_the_job_eligible(harness: Harness) -> None:
    job_id = (await upload_pdf(harness, key="deploys", text_value="Long job evidence")).json()[
        "job_id"
    ]
    for _ in range(3):
        provider = GatedEmbeddings()
        claim = await harness.claim()
        work = asyncio.create_task(
            process_job(claim=claim, context=harness.context(embeddings=provider))
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=5)
        work.cancel()
        provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
    row = await job_row(harness, UUID(job_id))
    assert row["cycle_attempts"] == 0
    assert row["status"] != JobState.FAILED
    await harness.process(await harness.claim())
    status = await harness.status.job_status(subject="owner", job_id=UUID(job_id))
    assert status.status == JobState.READY
    assert status.attempts == 1


async def test_shutdown_drains_running_work_before_exit(harness: Harness) -> None:
    await upload_pdf(harness, key="drain", text_value="Drained evidence")
    provider = GatedEmbeddings()
    wakeup, stop = asyncio.Event(), asyncio.Event()
    runtime = WorkerRuntime(
        telemetry=harness.telemetry,
        job=replace(
            harness.context(embeddings=provider),
            policy=replace(harness.policy, scan_interval_seconds=0.01, shutdown_grace_seconds=5),
        ),
        worker_id="draining-worker",
    )
    task = asyncio.create_task(serve(harness, runtime=runtime, wakeup=wakeup, stop=stop))
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    stop.set()
    wakeup.set()
    await asyncio.sleep(0.05)
    assert not task.done()
    provider.release.set()
    await asyncio.wait_for(task, timeout=5)
    jobs = await harness.status.list_documents(subject="owner", limit=10, offset=0)
    assert jobs[0].status == JobState.READY


async def test_corrupt_trace_context_ahead_of_a_valid_job_is_isolated(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    corrupt = (await upload_pdf(harness, key="poison", text_value="Corrupt row")).json()
    valid = (await upload_pdf(harness, key="healthy", text_value="Healthy row")).json()
    async with harness.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.ingestion_jobs SET trace_context='{\"traceparent\": 5}',"
                " next_retry_at=now()-interval '1 minute' WHERE id=:id"
            ),
            {"id": corrupt["job_id"]},
        )
    wakeup, stop = asyncio.Event(), asyncio.Event()
    runtime = WorkerRuntime(
        telemetry=harness.telemetry,
        job=replace(
            harness.context(),
            policy=replace(harness.policy, scan_interval_seconds=0.01, concurrency=1),
        ),
        worker_id="poison-test",
    )
    task = asyncio.create_task(serve(harness, runtime=runtime, wakeup=wakeup, stop=stop))
    try:
        async with asyncio.timeout(5):
            while True:
                job = await harness.status.job_status(subject="owner", job_id=UUID(valid["job_id"]))
                if job.status == JobState.READY:
                    break
                await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        stop.set()
        wakeup.set()
        await asyncio.wait_for(task, timeout=5)
    row = await job_row(harness, UUID(corrupt["job_id"]))
    assert row["status"] == JobState.FAILED
    assert row["error_category"] == ErrorCategory.INTEGRITY
    async with harness.engine.connect() as conn:
        version = await conn.scalar(
            text("SELECT status FROM app.document_versions WHERE id=:id"),
            {"id": corrupt["version_id"]},
        )
    assert version == "failed"
    assert "ingestion_job_corrupt" in [
        record.msg for record in caplog.records if record.levelno == logging.ERROR
    ]


async def test_database_outage_during_publication_is_not_a_failure(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await upload_pdf(harness, key="publish-outage", text_value="Outage evidence")
    claim = await harness.claim()
    publish = PgIndexStore.publish

    async def unreachable(self: PgIndexStore, *, claim: Claim) -> None:
        raise DependencyUnavailableError("database_unavailable")

    monkeypatch.setattr(PgIndexStore, "publish", unreachable)
    await harness.process(claim)
    row = await job_row(harness, claim.job_id)
    assert row["status"] == JobState.RETRYING
    assert row["error_category"] == ErrorCategory.STORAGE
    monkeypatch.setattr(PgIndexStore, "publish", publish)
    async with harness.engine.begin() as conn:
        await conn.execute(
            text("UPDATE app.ingestion_jobs SET next_retry_at=now() WHERE id=:id"),
            {"id": claim.job_id},
        )
    await harness.process(await harness.claim())
    status = await harness.status.job_status(subject="owner", job_id=claim.job_id)
    assert status.status == JobState.READY


async def test_stale_chunk_completion_leaves_the_job_unchanged(harness: Harness) -> None:
    await upload_pdf(harness, key="stale-chunk", text_value="Contended evidence")
    claim = await harness.claim()

    @dataclass(frozen=True, slots=True, kw_only=True)
    class CompetingOwner:
        """Another attempt settles the chunk while this attempt's provider call runs."""

        async def embed(self, *, text: str) -> tuple[float, ...]:
            async with harness.engine.begin() as conn:
                await conn.execute(
                    SETTLE_ELSEWHERE, {"vector": str(list(VECTOR)), "version": claim.version_id}
                )
            return VECTOR

    await process_job(claim=claim, context=harness.context(embeddings=CompetingOwner()))
    row = await job_row(harness, claim.job_id)
    assert row["status"] == JobState.PROCESSING
    assert row["error_category"] is None


async def test_upload_replay_and_status_report_the_same_retry_availability(
    harness: Harness,
) -> None:
    async def replay() -> dict[str, object]:
        response = await harness.client.post(
            "/v1/documents",
            files={"file": ("image.pdf", pdf_bytes(pages=("",)), "application/pdf")},
            headers={"Idempotency-Key": "agreement"},
        )
        result: dict[str, object] = response.json()
        return result

    accepted = await replay()
    await harness.process(await harness.claim())
    job_id = UUID(str(accepted["job_id"]))
    failed = await harness.status.job_status(subject="owner", job_id=job_id)
    assert failed.status == JobState.FAILED
    assert (await replay())["retry_available"] is failed.retry_available is True
    await harness.client.delete(f"/v1/documents/{accepted['document_id']}")
    deleting = await harness.status.job_status(subject="owner", job_id=job_id)
    assert (await replay())["retry_available"] is deleting.retry_available is False


class UnavailableEmbeddings:
    async def embed(self, *, text: str) -> tuple[float, ...]:
        raise EmbeddingUnavailableError("provider_unavailable")


async def no_wait(_: float) -> None:
    return None


async def test_a_released_claim_keeps_the_attempts_of_a_settled_chunk(harness: Harness) -> None:
    await upload_pdf(harness, key="last-call", text_value="Last call evidence")
    started = await harness.queue.start_attempt(claim=await harness.claim())
    with pytest.raises(EmbeddingUnavailableError):
        async with asyncio.timeout(60) as timeout:
            await index_version(
                claim=started,
                context=harness.context(embeddings=UnavailableEmbeddings(), sleep=no_wait),
                deadline=JobDeadline(timeout=timeout),
            )
    # A shutdown releases the claim before the job failure is recorded.
    await harness.queue.release(claim=started, attempt_started=True)
    async with harness.engine.connect() as conn:
        chunk = (
            (
                await conn.execute(
                    text(
                        "SELECT status, attempts, cycle_attempts FROM app.document_chunks"
                        " WHERE version_id=:version"
                    ),
                    {"version": started.version_id},
                )
            )
            .mappings()
            .one()
        )
    assert chunk["status"] == "retrying"
    assert chunk["attempts"] == chunk["cycle_attempts"] == harness.policy.max_call_attempts
