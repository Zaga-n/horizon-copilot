"""Upload preparation, immutable storage and atomic acceptance capabilities."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from horizon_ingestion.domain.acceptance import UploadMetadata
from horizon_ingestion.domain.documents import Acceptance, FileType, Pipeline
from horizon_ingestion.ports.errors import RejectedError


class UploadValidationError(RejectedError):
    """The file is malformed, unsupported or inconsistent with its declaration."""


class FileTooLargeError(UploadValidationError):
    """The upload exceeds the configured byte limit."""

    def __init__(self) -> None:
        super().__init__("file_too_large")


class ConflictError(Exception):
    """Idempotency input or a replacement conflicts with previously accepted work."""


class NotFoundError(Exception):
    """The private resource is absent or is owned by another subject."""


class UploadStream(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedUpload:
    path: Path
    sha256: str
    filename: str
    file_type: FileType


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectRef:
    key: str
    version_id: str
    created_at: datetime


class UploadPreparer(Protocol):
    def prepare(
        self, *, stream: UploadStream, filename: str, content_type: str
    ) -> AbstractAsyncContextManager[PreparedUpload]: ...


class ObjectStorage(Protocol):
    async def upload(self, *, path: Path, file_type: FileType) -> ObjectRef: ...
    async def exists(self, *, ref: ObjectRef) -> bool: ...
    async def remove(self, *, ref: ObjectRef) -> None: ...
    async def download(self, *, ref: ObjectRef, path: Path) -> None: ...
    async def orphan_candidates(self, *, limit: int) -> tuple[ObjectRef, ...]: ...


class AcceptanceStore(Protocol):
    async def find_existing(
        self, *, subject: str, key: str, sha256: str, metadata: UploadMetadata, pipeline: Pipeline
    ) -> Acceptance | None:
        """None means no replay/content match; conflicts and private misses raise."""
        ...

    async def accept(
        self,
        *,
        subject: str,
        key: str,
        upload: PreparedUpload,
        metadata: UploadMetadata,
        pipeline: Pipeline,
        ref: ObjectRef,
        max_object_age_seconds: float,
        trace_context: dict[str, str],
    ) -> Acceptance:
        """Commit only while the stored original is younger than `max_object_age_seconds`.

        Age is measured on the database clock, the clock orphan reconciliation uses.
        """
        ...
