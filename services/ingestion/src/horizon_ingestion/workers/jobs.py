"""Bounded durable job claims with heartbeat cancellation; the supervisor owns cadence and drain."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import assert_never

from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.application.process_job import (
    Failed,
    Fenced,
    JobOutcome,
    Ready,
    Retrying,
    Unrecorded,
    process_job,
)
from horizon_ingestion.domain.documents import JobKind
from horizon_ingestion.observability.tracing import Telemetry, mark_category_error
from horizon_ingestion.ports.errors import DependencyUnavailableError
from horizon_ingestion.ports.indexing import Claim, StaleClaimError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerRuntime:
    """Worker-owned dependencies are separate from the HTTP runtime view."""

    telemetry: Telemetry
    job: JobContext
    worker_id: str


async def _heartbeat(*, claim: Claim, runtime: WorkerRuntime) -> None:
    policy = runtime.job.policy
    while True:
        await asyncio.sleep(policy.heartbeat_interval_seconds)
        await runtime.job.queue.heartbeat(claim=claim, lease_seconds=policy.lease_seconds)


async def run_claim(*, claim: Claim, runtime: WorkerRuntime) -> None:
    with runtime.telemetry.work("job", carrier=claim.trace_context) as span:
        span.set_attributes(
            {
                "app.job.id": str(claim.job_id),
                "app.document.id": str(claim.document_id),
                "app.message.attempt": claim.attempts,
            }
        )
        runtime.telemetry.measurements.queue_age.record(
            max(0, (datetime.now(UTC) - claim.created_at).total_seconds()),
            {"kind": claim.kind.value},
        )
        outcome = await _run_claim(claim=claim, runtime=runtime)
        if outcome is not None:
            _report(outcome, telemetry=runtime.telemetry)


async def _run_claim(*, claim: Claim, runtime: WorkerRuntime) -> JobOutcome | None:
    """The attempt's outcome; None when the heartbeat lost the lease and stopped it."""
    heartbeat = asyncio.create_task(_heartbeat(claim=claim, runtime=runtime))
    work = asyncio.create_task(process_job(claim=claim, context=runtime.job))
    try:
        await asyncio.wait((heartbeat, work), return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await heartbeat
    except StaleClaimError:
        log.info(
            "ingestion_claim_fenced",
            extra={"job_id": str(claim.job_id), "generation": claim.generation},
        )
    except DependencyUnavailableError:
        # The lease cannot be renewed; the job becomes due again once it expires.
        log.warning("ingestion_heartbeat_unavailable", extra={"job_id": str(claim.job_id)})
    except Exception:  # A heartbeat defect: the lease expires and the job re-runs.
        log.exception("ingestion_heartbeat_failed", extra={"job_id": str(claim.job_id)})
    finally:
        heartbeat.cancel()
        work.cancel()
        await asyncio.gather(heartbeat, work, return_exceptions=True)
    return None


def _report(outcome: JobOutcome, *, telemetry: Telemetry) -> None:
    """The attempt's one outcome log and job metrics."""
    claim = outcome.claim
    fields: dict[str, str | int] = {"job_id": str(claim.job_id), "attempt": claim.attempts}
    jobs = telemetry.measurements.jobs
    match outcome:
        case Ready():
            jobs.add(1, {"state": "ready", "kind": claim.kind.value})
            ready = (
                "ingestion_job_ready"
                if claim.kind == JobKind.INDEX
                else "ingestion_cleanup_complete"
            )
            log.info(ready, extra={**fields, "kind": claim.kind.value})
        case Retrying(category=category):
            mark_category_error(category.value)
            jobs.add(1, {"state": "retrying", "kind": claim.kind.value})
            telemetry.measurements.retries.add(1, {"scope": "job", "category": category.value})
            log.warning("ingestion_job_retry", extra={**fields, "category": category.value})
        case Failed(category=category, failure=failure):
            mark_category_error(category.value)
            jobs.add(1, {"state": "failed", "kind": claim.kind.value})
            log.error(
                "ingestion_job_failed",
                extra={**fields, "category": category.value},
                exc_info=failure,
            )
        case Fenced():
            log.info(
                "ingestion_claim_fenced",
                extra={"job_id": str(claim.job_id), "generation": claim.generation},
            )
        case Unrecorded(category=category, failure=failure):
            log.warning(
                "ingestion_failure_record_unavailable",
                extra={"job_id": str(claim.job_id), "category": category.value},
                exc_info=failure,
            )
        case _:
            assert_never(outcome)


@dataclass(slots=True, kw_only=True)
class JobDispatcher:
    """Owns the running claim tasks; mutated by `claim_due` and the tasks' done callbacks."""

    runtime: WorkerRuntime
    wakeup: asyncio.Event
    running: set[asyncio.Task[None]] = field(default_factory=set)

    async def claim_due(self) -> None:
        """Claim up to the free capacity and start each claim; an outage raises."""
        self.wakeup.clear()
        policy = self.runtime.job.policy
        capacity = policy.concurrency - len(self.running)
        if capacity <= 0:
            return
        claims = await self.runtime.job.queue.claim(
            worker_id=self.runtime.worker_id, limit=capacity, lease_seconds=policy.lease_seconds
        )
        for claim in claims:
            task = asyncio.create_task(run_claim(claim=claim, runtime=self.runtime))
            self.running.add(task)
            task.add_done_callback(self.running.discard)
            # A finished claim frees capacity, so look for more work right away.
            task.add_done_callback(lambda completed: self.wakeup.set())

    async def drain(self, *, grace_seconds: float) -> None:
        """Let running claims settle within the grace period, then cancel so they release."""
        if self.running:
            await asyncio.wait(tuple(self.running), timeout=grace_seconds)
        remaining = tuple(self.running)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
