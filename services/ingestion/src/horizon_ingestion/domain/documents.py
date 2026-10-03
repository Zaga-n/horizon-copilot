"""Ingestion lifecycle values and immutable process policies."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Persisted and public values reject unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRYING = "retrying"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    INDEX = "index"
    DELETE = "delete"
    SUPERSEDED_CLEANUP = "superseded_cleanup"


class Stage(StrEnum):
    EXTRACTION = "extraction"
    EMBEDDING = "embedding"
    PUBLICATION = "publication"
    CLEANUP = "cleanup"
    DONE = "done"


class VersionState(StrEnum):
    CANDIDATE = "candidate"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class ChunkState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class Lifecycle(StrEnum):
    LIVE = "live"
    DELETING = "deleting"
    DELETED = "deleted"


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"


class ErrorCategory(StrEnum):
    STORAGE = "storage_unavailable"
    PROVIDER = "provider_unavailable"
    PROTOCOL = "provider_protocol"
    REJECTED = "provider_rejected"
    EXTRACTION = "extraction_failed"
    NO_TEXT = "no_extractable_text"
    MANIFEST = "manifest_incomplete"
    INTEGRITY = "integrity_failed"
    BUDGET = "budget_exhausted"
    INTERNAL = "internal"


class Locator(StrictModel):
    page: Annotated[int, Field(gt=0)] | None = None
    section_heading: str | None = None
    start_offset: Annotated[int, Field(ge=0)] | None = None
    end_offset: Annotated[int, Field(ge=0)] | None = None


class Pipeline(StrictModel):
    parser_version: Literal["pypdf-6.19.0/docx-1.2.0"] = "pypdf-6.19.0/docx-1.2.0"
    chunker_version: Literal["fixed-window-v1"] = "fixed-window-v1"
    # Widened, never replaced: stored configurations of versions indexed under an older
    # normalization must keep decoding, and resuming them must rebuild the same manifest.
    normalization: Literal["crlf-cr-to-lf", "crlf-cr-to-lf+c0"] = "crlf-cr-to-lf+c0"
    separator: Literal["\n"] = "\n"
    units: Literal["unicode-code-points"] = "unicode-code-points"
    window_size: Annotated[int, Field(gt=0)]
    overlap: Annotated[int, Field(ge=0)]
    embedding_model_id: Literal["amazon.titan-embed-text-v2:0"]
    embedding_dimensions: Literal[1024] = 1024


class JobStatus(StrictModel):
    document_id: UUID
    filename: str | None
    version_id: UUID | None
    job_id: UUID
    status: JobState
    stage: Stage
    lifecycle: Lifecycle
    total_chunks: int | None
    completed_chunks: int
    retrying_chunks: int
    failed_chunks: int
    attempts: int
    retry_cycle: int
    error_category: ErrorCategory | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    retry_available: bool


class Acceptance(StrictModel):
    document_id: UUID
    version_id: UUID
    job_id: UUID
    status: JobState
    deduplicated: bool
    retry_available: bool


class Deletion(StrictModel):
    document_id: UUID
    lifecycle: Lifecycle


# Bounds the claim hand-back write while the worker shuts down; part of the stop budget.
CLAIM_RELEASE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerPolicy:
    chunk_timeout_seconds: float
    job_timeout_seconds: float
    concurrency: int
    vendor_concurrency: int
    scan_interval_seconds: float
    lease_seconds: float
    heartbeat_interval_seconds: float
    permit_lease_seconds: float
    max_call_attempts: int
    max_chunk_attempts: int
    max_job_attempts: int
    shutdown_grace_seconds: float
    retry_initial_backoff_seconds: float
    retry_max_backoff_seconds: float
    reconciliation_interval_seconds: float
    orphan_grace_seconds: float
    maintenance_batch_size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionPolicy:
    memory_bytes: int
    max_chars: int
    max_units: int
    timeout_seconds: float
