"""Durable indexing claims, manifests and fenced result writes."""

from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import AwareDatetime

from horizon_ingestion.domain.chunking import Chunk, Extraction
from horizon_ingestion.domain.documents import (
    ErrorCategory,
    FileType,
    JobKind,
    JobStatus,
    Pipeline,
    StrictModel,
)
from horizon_ingestion.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)
from horizon_ingestion.ports.uploads import ObjectRef


class StaleClaimError(Exception):
    """The lease expired, was superseded, or was cancelled by deletion."""


class ChunkNotProcessingError(StaleClaimError):
    """Another attempt already settled or reset the chunk this attempt tried to complete."""

    def __init__(self) -> None:
        super().__init__("chunk_not_processing")


class ChunkBudgetExhaustedError(Exception):
    """The chunk used every physical attempt allowed in this retry cycle."""

    def __init__(self) -> None:
        super().__init__("chunk_budget_exhausted")


class IndexStoreIntegrityError(DataIntegrityError):
    """A stored job, version, chunk or status row no longer decodes into its contract."""

    def __init__(self, error_code: str, *, row_id: UUID) -> None:
        super().__init__(error_code)
        self.row_id = row_id


class ManifestIncompleteError(DataIntegrityError):
    """Publication found chunks that are missing or not yet embedded."""

    def __init__(self) -> None:
        super().__init__("manifest_incomplete")


class EmptyManifestError(RejectedError):
    """The extracted document produced no chunks to index."""

    def __init__(self) -> None:
        super().__init__("empty_manifest")


class EmbeddingUnavailableError(DependencyUnavailableError):
    """The provider throttled, failed, timed out, or refused our credentials or model."""


class EmbeddingProtocolError(DependencyUnavailableError):
    """The provider answered successfully with a response that is not a usable vector."""


class EmbeddingRejectedError(RejectedError):
    """The provider rejected this input itself; resending it fails the same way."""


class Claim(StrictModel):
    created_at: AwareDatetime
    job_id: UUID
    document_id: UUID
    version_id: UUID | None
    kind: JobKind
    generation: int
    lease_owner: str
    attempts: int
    cycle_attempts: int
    trace_context: dict[str, str]


class VersionWork(StrictModel):
    version_id: UUID
    filename: str
    file_type: FileType
    pipeline: Pipeline
    object_key: str
    object_version_id: str
    manifest_complete: bool


class ChunkWork(StrictModel):
    next_retry_at: AwareDatetime | None
    chunk_id: UUID
    text: str
    attempts: int
    cycle_attempts: int


class ExtractionPort(Protocol):
    async def extract(self, *, path: Path, file_type: FileType, filename: str) -> Extraction: ...


class EmbeddingPort(Protocol):
    async def embed(self, *, text: str) -> tuple[float, ...]: ...


class IndexStore(Protocol):
    async def version(self, *, claim: Claim) -> VersionWork: ...
    async def save_manifest(self, *, claim: Claim, chunks: tuple[Chunk, ...]) -> None: ...
    async def unfinished(self, *, claim: Claim) -> tuple[ChunkWork, ...]: ...
    async def begin_chunk(self, *, claim: Claim, chunk_id: UUID, max_attempts: int) -> None: ...
    async def complete_chunk(
        self, *, claim: Claim, chunk_id: UUID, vector: tuple[float, ...]
    ) -> None: ...
    async def retry_chunk(
        self, *, claim: Claim, chunk_id: UUID, category: ErrorCategory, delay_seconds: float
    ) -> None: ...
    async def publish(self, *, claim: Claim) -> None: ...


class CleanupStore(Protocol):
    """Exact originals of a deleted document or superseded version, then their tombstones."""

    async def cleanup_refs(self, *, claim: Claim) -> tuple[ObjectRef, ...]: ...
    async def finish_cleanup(self, *, claim: Claim) -> None: ...


class WorkQueue(Protocol):
    async def claim(
        self, *, worker_id: str, limit: int, lease_seconds: float
    ) -> tuple[Claim, ...]: ...
    async def start_attempt(self, *, claim: Claim) -> Claim:
        """Count one started attempt against the budget; returns the claim with new counters."""
        ...

    async def release(self, *, claim: Claim, attempt_started: bool) -> None:
        """Hand an unsettled claim back for recovery; a started attempt is uncounted again."""
        ...

    async def heartbeat(self, *, claim: Claim, lease_seconds: float) -> None: ...
    async def fail(
        self, *, claim: Claim, category: ErrorCategory, delay_seconds: float | None
    ) -> None: ...
    async def acquire_permit(self, *, token: UUID, cap: int, lease_seconds: float) -> int | None:
        """None means all configured vendor slots have unexpired leases."""
        ...

    async def release_permit(self, *, slot: int, token: UUID) -> None: ...


class StatusNotFoundError(Exception):
    """The job or document is absent or owned by another subject."""


class StatusConflictError(Exception):
    """A retry cannot start: unavailable for this job, or another index job is active."""


class StatusStore(Protocol):
    """Raises StatusNotFoundError and StatusConflictError."""

    async def document_status(self, *, subject: str, document_id: UUID) -> JobStatus: ...
    async def job_status(self, *, subject: str, job_id: UUID) -> JobStatus: ...
    async def list_documents(
        self, *, subject: str, limit: int, offset: int
    ) -> tuple[JobStatus, ...]: ...
    async def retry(self, *, subject: str, job_id: UUID) -> JobStatus: ...
    async def delete(self, *, subject: str, document_id: UUID) -> None: ...
