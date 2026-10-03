"""The failure vocabulary of one job attempt: each failure type names its error category."""

from horizon_ingestion.domain.chunking import NoExtractableTextError
from horizon_ingestion.domain.documents import ErrorCategory
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
from horizon_ingestion.ports.uploads import UploadValidationError


class ChunkDeadlineExceededError(Exception):
    """One chunk spent its provider-time budget; waits for permits and retries are excluded."""


class JobDeadlineExceededError(Exception):
    """The whole job exceeded its wall-clock budget, excluding permit and backoff waits."""


# Ordered: the first matching type names the cause, so subclasses precede their bases.
FAILURE_CATEGORIES: tuple[tuple[type[Exception], ErrorCategory], ...] = (
    (NoExtractableTextError, ErrorCategory.NO_TEXT),
    (EmptyManifestError, ErrorCategory.NO_TEXT),
    (UploadValidationError, ErrorCategory.EXTRACTION),
    (EmbeddingRejectedError, ErrorCategory.REJECTED),
    (EmbeddingProtocolError, ErrorCategory.PROTOCOL),
    (EmbeddingUnavailableError, ErrorCategory.PROVIDER),
    (ChunkDeadlineExceededError, ErrorCategory.PROVIDER),
    (ChunkBudgetExhaustedError, ErrorCategory.BUDGET),
    (JobDeadlineExceededError, ErrorCategory.BUDGET),
    (ManifestIncompleteError, ErrorCategory.MANIFEST),
    (DataIntegrityError, ErrorCategory.INTEGRITY),
    (RejectedError, ErrorCategory.INTEGRITY),
    (DependencyUnavailableError, ErrorCategory.STORAGE),
)
HANDLED_FAILURES = tuple(error for error, _ in FAILURE_CATEGORIES)


def failure_category(exc: Exception) -> ErrorCategory:
    return next(
        (category for error, category in FAILURE_CATEGORIES if isinstance(exc, error)),
        ErrorCategory.INTERNAL,
    )
