"""Run one job attempt: count it, run index or cleanup, settle its failure once, and report it."""

import asyncio
import logging
from dataclasses import dataclass

from horizon_ingestion.application.cleanup_document import cleanup_document
from horizon_ingestion.application.failures import (
    HANDLED_FAILURES,
    JobDeadlineExceededError,
    failure_category,
)
from horizon_ingestion.application.index_version import JobDeadline, index_version
from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.domain.documents import (
    CLAIM_RELEASE_TIMEOUT_SECONDS,
    ErrorCategory,
    JobKind,
)
from horizon_ingestion.domain.retry_policy import (
    Retry,
    attempt_budget_spent,
    decide_job_failure,
)
from horizon_ingestion.ports.errors import DependencyUnavailableError
from horizon_ingestion.ports.indexing import Claim, StaleClaimError, WorkQueue

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Ready:
    """The index version was published, or the cleanup finished."""

    claim: Claim


@dataclass(frozen=True, slots=True, kw_only=True)
class Retrying:
    claim: Claim
    category: ErrorCategory
    delay_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Failed:
    claim: Claim
    category: ErrorCategory
    failure: Exception | None  # None when the budget ran out before the attempt did any work


@dataclass(frozen=True, slots=True, kw_only=True)
class Fenced:
    """The claim went stale (lease lost or document deleted); another owner settles the job."""

    claim: Claim


@dataclass(frozen=True, slots=True, kw_only=True)
class Unrecorded:
    """The failure could not be recorded; the lease expiry re-runs the job."""

    claim: Claim
    category: ErrorCategory
    failure: Exception


type JobOutcome = Ready | Retrying | Failed | Fenced | Unrecorded


async def process_job(*, claim: Claim, context: JobContext) -> JobOutcome:
    """Settle one attempt exactly once; only cancellation propagates, after releasing the claim.

    Every failure is recorded against the started claim, so its counters decide the retry.
    """
    started: Claim | None = None
    try:
        started = await context.queue.start_attempt(claim=claim)
        if attempt_budget_spent(
            kind=started.kind, attempt=started.cycle_attempts, policy=context.policy
        ):
            return await _record_failure(
                claim=started, category=ErrorCategory.BUDGET, context=context
            )
        await _attempt(claim=started, context=context)
    except asyncio.CancelledError:
        await _release(queue=context.queue, claim=started or claim, attempt_started=bool(started))
        raise
    except StaleClaimError:
        return Fenced(claim=started or claim)
    except HANDLED_FAILURES as exc:
        return await _record_failure(
            claim=started or claim,
            category=failure_category(exc),
            context=context,
            retry_after_seconds=exc.retry_after_seconds
            if isinstance(exc, DependencyUnavailableError)
            else None,
            failure=exc,
        )
    except Exception as exc:  # noqa: BLE001 Job boundary: a defect is settled once as internal; the worker logs it with its traceback.
        return await _record_failure(
            claim=started or claim, category=ErrorCategory.INTERNAL, context=context, failure=exc
        )
    return Ready(claim=started)


async def _attempt(*, claim: Claim, context: JobContext) -> None:
    try:
        async with asyncio.timeout(context.policy.job_timeout_seconds) as timeout:
            if claim.kind == JobKind.INDEX:
                await index_version(
                    claim=claim, context=context, deadline=JobDeadline(timeout=timeout)
                )
            else:
                await cleanup_document(claim=claim, context=context)
    except TimeoutError as exc:
        raise JobDeadlineExceededError() from exc


async def _record_failure(
    *,
    claim: Claim,
    category: ErrorCategory,
    context: JobContext,
    retry_after_seconds: float | None = None,
    failure: Exception | None = None,
) -> JobOutcome:
    """Apply the retry policy once and settle the job accordingly."""
    decision = decide_job_failure(
        category=category,
        kind=claim.kind,
        attempt=claim.cycle_attempts,
        policy=context.policy,
        jitter=context.jitter,
        retry_after_seconds=retry_after_seconds,
    )
    delay = decision.delay_seconds if isinstance(decision, Retry) else None
    try:
        await context.queue.fail(claim=claim, category=category, delay_seconds=delay)
    except (DependencyUnavailableError, StaleClaimError) as exc:
        # The lease expiry re-runs the job; keep the original failure's type for diagnosis.
        return Unrecorded(claim=claim, category=category, failure=failure or exc)
    if delay is not None:
        return Retrying(claim=claim, category=category, delay_seconds=delay)
    return Failed(claim=claim, category=category, failure=failure)


async def _release(*, queue: WorkQueue, claim: Claim, attempt_started: bool) -> None:
    try:
        async with asyncio.timeout(CLAIM_RELEASE_TIMEOUT_SECONDS):
            await queue.release(claim=claim, attempt_started=attempt_started)
    except (TimeoutError, DependencyUnavailableError, StaleClaimError):
        # The lease still expires on its own; only the prompt hand-back is lost.
        log.warning("ingestion_claim_release_unavailable", extra={"job_id": str(claim.job_id)})
