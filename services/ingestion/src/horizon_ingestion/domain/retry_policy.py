"""The single decision between a bounded retry and a terminal failure for one job attempt."""

from collections.abc import Callable
from dataclasses import dataclass

from horizon_ingestion.domain.documents import ErrorCategory, JobKind, WorkerPolicy

type Jitter = Callable[[float, float], float]
"""Draw a value in [low, high]; production passes random.uniform."""

# Content-caused or exhausted outcomes; retrying the same input cannot change them.
TERMINAL_CATEGORIES = frozenset(
    {
        ErrorCategory.REJECTED,
        ErrorCategory.EXTRACTION,
        ErrorCategory.NO_TEXT,
        ErrorCategory.INTEGRITY,
        ErrorCategory.BUDGET,
    }
)
MAX_BACKOFF_DOUBLINGS = 20  # keeps 2**n finite long before the max backoff cap applies


@dataclass(frozen=True, slots=True, kw_only=True)
class Retry:
    category: ErrorCategory
    delay_seconds: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Terminal:
    category: ErrorCategory


def backoff(
    *, attempt: int, policy: WorkerPolicy, jitter: Jitter, retry_after_seconds: float | None = None
) -> float:
    """Full-jitter exponential delay, never shorter than the provider's retry-after hint."""
    bound = min(
        policy.retry_max_backoff_seconds,
        policy.retry_initial_backoff_seconds * (2 ** min(attempt - 1, MAX_BACKOFF_DOUBLINGS)),
    )
    return min(
        policy.retry_max_backoff_seconds,
        max(jitter(bound / 2, bound), retry_after_seconds or 0),
    )


def attempt_budget_spent(*, kind: JobKind, attempt: int, policy: WorkerPolicy) -> bool:
    """Whether an index job already started more attempts than its cycle allows.

    Attempts that crashed before recording a failure count too, so a job that kills its worker
    still ends. Cleanup has no such bound: its exit is a permanent failure category.
    """
    return kind == JobKind.INDEX and attempt > policy.max_job_attempts


def decide_job_failure(
    *,
    category: ErrorCategory,
    kind: JobKind,
    attempt: int,
    policy: WorkerPolicy,
    jitter: Jitter,
    retry_after_seconds: float | None = None,
) -> Retry | Terminal:
    """Indexing retries transient causes until the job budget is spent.

    Cleanup must finish eventually, so transient causes always retry with backoff; a permanent
    cause ends it once the same budget is spent, which lets a repeated delete re-queue it.
    """
    permanent = category in TERMINAL_CATEGORIES
    spent = attempt >= policy.max_job_attempts
    if (kind == JobKind.INDEX and (permanent or spent)) or (permanent and spent):
        return Terminal(category=category)
    return Retry(
        category=category,
        delay_seconds=backoff(
            attempt=attempt,
            policy=policy,
            jitter=jitter,
            retry_after_seconds=retry_after_seconds,
        ),
    )
