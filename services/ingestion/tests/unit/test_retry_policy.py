"""One retry decision: transient causes retry within the budget, content causes end the job."""

from dataclasses import replace

import pytest

from horizon_ingestion.application.failures import (
    ChunkDeadlineExceededError,
    JobDeadlineExceededError,
    failure_category,
)
from horizon_ingestion.db.transactions import translate_database_error
from horizon_ingestion.domain.chunking import NoExtractableTextError
from horizon_ingestion.domain.documents import ErrorCategory, JobKind
from horizon_ingestion.domain.retry_policy import (
    Retry,
    Terminal,
    attempt_budget_spent,
    decide_job_failure,
)
from horizon_ingestion.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)
from horizon_ingestion.ports.indexing import (
    ChunkBudgetExhaustedError,
    EmbeddingProtocolError,
    EmbeddingRejectedError,
    EmbeddingUnavailableError,
    EmptyManifestError,
    ManifestIncompleteError,
)
from horizon_ingestion.ports.uploads import FileTooLargeError, UploadValidationError
from horizon_ingestion_testing.jobs import POLICY


def highest(low: float, high: float) -> float:
    return high


@pytest.mark.parametrize(
    ("error", "category"),
    [
        pytest.param(NoExtractableTextError("no_extractable_text"), ErrorCategory.NO_TEXT),
        pytest.param(EmptyManifestError(), ErrorCategory.NO_TEXT),
        pytest.param(UploadValidationError("parser_timeout"), ErrorCategory.EXTRACTION),
        pytest.param(FileTooLargeError(), ErrorCategory.EXTRACTION),
        pytest.param(EmbeddingRejectedError("provider_rejected"), ErrorCategory.REJECTED),
        pytest.param(EmbeddingProtocolError("provider_protocol"), ErrorCategory.PROTOCOL),
        pytest.param(EmbeddingUnavailableError("provider_unavailable"), ErrorCategory.PROVIDER),
        pytest.param(ChunkDeadlineExceededError(), ErrorCategory.PROVIDER),
        pytest.param(ChunkBudgetExhaustedError(), ErrorCategory.BUDGET),
        pytest.param(JobDeadlineExceededError(), ErrorCategory.BUDGET),
        pytest.param(ManifestIncompleteError(), ErrorCategory.MANIFEST),
        pytest.param(DataIntegrityError("database_integrity"), ErrorCategory.INTEGRITY),
        pytest.param(RejectedError("database_rejected"), ErrorCategory.INTEGRITY),
        pytest.param(DependencyUnavailableError("database_unavailable"), ErrorCategory.STORAGE),
        pytest.param(RuntimeError("bug"), ErrorCategory.INTERNAL),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_each_failure_type_names_its_cause(error: Exception, category: ErrorCategory) -> None:
    assert failure_category(error) == category


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.PROVIDER,  # chunk deadline, throttling, denied or expired credentials
        ErrorCategory.STORAGE,  # database or object-storage outage
        ErrorCategory.PROTOCOL,
        ErrorCategory.MANIFEST,
        ErrorCategory.INTERNAL,
    ],
)
def test_transient_causes_retry_while_budget_remains(category: ErrorCategory) -> None:
    decision = decide_job_failure(
        category=category, kind=JobKind.INDEX, attempt=1, policy=POLICY, jitter=highest
    )
    assert decision == Retry(category=category, delay_seconds=0.5)


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.NO_TEXT,
        ErrorCategory.EXTRACTION,
        ErrorCategory.REJECTED,
        ErrorCategory.INTEGRITY,
        ErrorCategory.BUDGET,
    ],
)
def test_content_causes_are_terminal_on_the_first_attempt(category: ErrorCategory) -> None:
    decision = decide_job_failure(
        category=category, kind=JobKind.INDEX, attempt=1, policy=POLICY, jitter=highest
    )
    assert decision == Terminal(category=category)


def test_exhausted_budget_records_the_actual_cause_not_budget() -> None:
    decision = decide_job_failure(
        category=ErrorCategory.PROVIDER,
        kind=JobKind.INDEX,
        attempt=POLICY.max_job_attempts,
        policy=POLICY,
        jitter=highest,
    )
    assert decision == Terminal(category=ErrorCategory.PROVIDER)


def test_cleanup_retries_even_after_the_index_budget() -> None:
    decision = decide_job_failure(
        category=ErrorCategory.STORAGE,
        kind=JobKind.DELETE,
        attempt=50,
        policy=POLICY,
        jitter=highest,
    )
    assert isinstance(decision, Retry)


@pytest.mark.parametrize("kind", [JobKind.SUPERSEDED_CLEANUP, JobKind.DELETE])
def test_permanent_cleanup_faults_end_once_the_budget_is_spent(kind: JobKind) -> None:
    def decide(attempt: int) -> Retry | Terminal:
        return decide_job_failure(
            category=ErrorCategory.INTEGRITY,
            kind=kind,
            attempt=attempt,
            policy=POLICY,
            jitter=highest,
        )

    assert isinstance(decide(POLICY.max_job_attempts - 1), Retry)
    assert decide(POLICY.max_job_attempts) == Terminal(category=ErrorCategory.INTEGRITY)


def test_only_index_jobs_stop_on_started_attempts_alone() -> None:
    over = POLICY.max_job_attempts + 1
    assert attempt_budget_spent(kind=JobKind.INDEX, attempt=over, policy=POLICY)
    assert not attempt_budget_spent(
        kind=JobKind.INDEX, attempt=POLICY.max_job_attempts, policy=POLICY
    )
    assert not attempt_budget_spent(kind=JobKind.DELETE, attempt=over, policy=POLICY)


def test_backoff_honours_provider_retry_after_within_the_cap() -> None:
    policy = replace(POLICY, retry_max_backoff_seconds=4)
    decision = decide_job_failure(
        category=ErrorCategory.PROVIDER,
        kind=JobKind.INDEX,
        attempt=1,
        policy=policy,
        jitter=highest,
        retry_after_seconds=60,
    )
    assert decision == Retry(category=ErrorCategory.PROVIDER, delay_seconds=4)


def test_unknown_driver_failures_are_not_labelled_outages() -> None:
    assert translate_database_error(RuntimeError("bug")) is None
