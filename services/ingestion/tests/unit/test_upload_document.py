"""Upload acceptance: replays skip storage; new uploads store before committing."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from horizon_ingestion.application.upload_document import upload_document
from horizon_ingestion.domain.acceptance import UploadMetadata
from horizon_ingestion.domain.documents import Acceptance, FileType, JobState, Pipeline
from horizon_ingestion.ports.uploads import (
    ObjectRef,
    PreparedUpload,
    UploadStream,
    UploadValidationError,
)

PIPELINE = Pipeline(
    window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
)


def acceptance(*, deduplicated: bool) -> Acceptance:
    return Acceptance(
        document_id=uuid4(),
        version_id=uuid4(),
        job_id=uuid4(),
        status=JobState.QUEUED,
        deduplicated=deduplicated,
        retry_available=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SpooledPreparer:
    path: Path

    @asynccontextmanager
    async def prepare(
        self, *, stream: UploadStream, filename: str, content_type: str
    ) -> AsyncIterator[PreparedUpload]:
        yield PreparedUpload(
            path=self.path, sha256="a" * 64, filename=filename, file_type=FileType.PDF
        )


@dataclass(slots=True, kw_only=True)
class RecordingStorage:
    uploads: list[Path] = field(default_factory=list)

    async def upload(self, *, path: Path, file_type: FileType) -> ObjectRef:
        self.uploads.append(path)
        return ObjectRef(key="attempts/a.pdf", version_id="v1", created_at=datetime.now(UTC))

    async def exists(self, *, ref: ObjectRef) -> bool:
        return True

    async def remove(self, *, ref: ObjectRef) -> None:
        raise NotImplementedError

    async def download(self, *, ref: ObjectRef, path: Path) -> None:
        raise NotImplementedError

    async def orphan_candidates(self, *, limit: int) -> tuple[ObjectRef, ...]:
        return ()


@dataclass(slots=True, kw_only=True)
class RecordingAcceptance:
    existing: Acceptance | None
    max_ages: list[float] = field(default_factory=list)

    async def find_existing(self, **_: object) -> Acceptance | None:
        return self.existing

    async def accept(self, *, max_object_age_seconds: float, **_: object) -> Acceptance:
        self.max_ages.append(max_object_age_seconds)
        return acceptance(deduplicated=False)


class EmptyStream:
    async def read(self, size: int = -1) -> bytes:
        return b""


async def upload(
    tmp_path: Path, *, store: RecordingAcceptance, storage: RecordingStorage, key: str = "k"
) -> Acceptance:
    return await upload_document(
        subject="owner",
        key=key,
        stream=EmptyStream(),
        filename="a.pdf",
        content_type="application/pdf",
        metadata=UploadMetadata(),
        pipeline=PIPELINE,
        preparer=SpooledPreparer(path=tmp_path / "a.pdf"),
        storage=storage,
        store=store,
        timeout_seconds=120,
        trace_context={},
    )


async def test_a_replay_returns_the_existing_job_without_storing(tmp_path: Path) -> None:
    existing = acceptance(deduplicated=True)
    store, storage = RecordingAcceptance(existing=existing), RecordingStorage()
    assert await upload(tmp_path, store=store, storage=storage) == existing
    assert storage.uploads == []
    assert store.max_ages == []


async def test_a_new_upload_is_stored_then_accepted_within_the_upload_timeout(
    tmp_path: Path,
) -> None:
    store, storage = RecordingAcceptance(existing=None), RecordingStorage()
    result = await upload(tmp_path, store=store, storage=storage)
    assert not result.deduplicated
    assert storage.uploads == [tmp_path / "a.pdf"]
    assert store.max_ages == [120]


async def test_an_out_of_range_idempotency_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UploadValidationError):
        await upload(
            tmp_path,
            store=RecordingAcceptance(existing=None),
            storage=RecordingStorage(),
            key="k" * 201,
        )
