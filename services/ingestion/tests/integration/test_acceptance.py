"""Committed upload IDs survive replay/concurrency and remain owner-private."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from mypy_boto3_s3 import S3Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.db.acceptance import PgAcceptanceStore
from horizon_ingestion.db.reconciliation import OrphanReconciliation
from horizon_ingestion.db.status import PgStatusStore
from horizon_ingestion.domain.acceptance import UploadMetadata
from horizon_ingestion.domain.documents import Acceptance, FileType, Pipeline
from horizon_ingestion.ports.errors import DependencyUnavailableError
from horizon_ingestion.ports.indexing import StatusNotFoundError
from horizon_ingestion.ports.uploads import (
    ConflictError,
    ObjectRef,
    PreparedUpload,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True, kw_only=True)
class Stores:
    acceptance: PgAcceptanceStore
    status: PgStatusStore
    storage: MinioStorage
    engine: AsyncEngine


@pytest.fixture
async def stores(migrated_database: str, minio_client: S3Client) -> AsyncIterator[Stores]:
    engine = create_async_engine(
        migrated_database.replace("postgresql://", "postgresql+psycopg://")
    )
    storage = MinioStorage(
        client=minio_client,
        bucket="horizon-documents",
        prefix=f"attempts/{uuid4().hex}/",
        max_bytes=10000,
    )
    try:
        yield Stores(
            acceptance=PgAcceptanceStore(engine=engine, storage=storage),
            status=PgStatusStore(engine=engine),
            storage=storage,
            engine=engine,
        )
    finally:
        for ref in await storage.orphan_candidates(limit=100):
            await storage.remove(ref=ref)
        await engine.dispose()


def pipeline() -> Pipeline:
    return Pipeline(
        window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
    )


async def accept(
    stores: Stores,
    *,
    path: Path,
    subject: str,
    key: str,
    metadata: UploadMetadata | None = None,
    configured: Pipeline | None = None,
) -> Acceptance:
    ref = await stores.storage.upload(path=path, file_type=FileType.PDF)
    return await stores.acceptance.accept(
        subject=subject,
        key=key,
        upload=PreparedUpload(path=path, sha256="a" * 64, filename="a.pdf", file_type=FileType.PDF),
        metadata=metadata or UploadMetadata(),
        pipeline=configured or pipeline(),
        ref=ref,
        max_object_age_seconds=60,
        trace_context={},
    )


async def test_concurrent_duplicates_commit_one_job(stores: Stores, tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"original-file-bytes")
    first, second = await asyncio.gather(
        accept(stores, path=path, subject="owner", key="one"),
        accept(stores, path=path, subject="owner", key="two"),
    )
    assert first.document_id == second.document_id
    assert first.job_id == second.job_id
    assert sorted((first.deduplicated, second.deduplicated)) == [False, True]
    async with stores.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM app.ingestion_jobs")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM app.upload_requests")) == 2
    replay = await stores.acceptance.find_existing(
        subject="owner", key="one", sha256="a" * 64, metadata=UploadMetadata(), pipeline=pipeline()
    )
    assert replay is not None
    assert replay.job_id == first.job_id
    status = await stores.status.job_status(subject="owner", job_id=first.job_id)
    assert status.total_chunks is None
    assert status.completed_chunks == 0
    with pytest.raises(StatusNotFoundError, match="job_not_found"):
        await stores.status.job_status(subject="intruder", job_id=first.job_id)
    private = await accept(stores, path=path, subject="intruder", key="one")
    assert private.document_id != first.document_id


async def test_lost_acknowledgment_and_changed_key_input(stores: Stores, tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"original-file-bytes")
    accepted = await accept(stores, path=path, subject="owner", key="lost-response")
    repeated = await accept(stores, path=path, subject="owner", key="lost-response")
    assert repeated.job_id == accepted.job_id
    with pytest.raises(ConflictError, match="idempotency_key_conflict"):
        await stores.acceptance.find_existing(
            subject="owner",
            key="lost-response",
            sha256="b" * 64,
            metadata=UploadMetadata(),
            pipeline=pipeline(),
        )
    configured = Pipeline(
        window_size=1000, overlap=100, embedding_model_id="amazon.titan-embed-text-v2:0"
    )
    changed = await accept(
        stores, path=path, subject="owner", key="new-pipeline", configured=configured
    )
    assert changed.version_id != accepted.version_id


async def test_expired_finalization_leaves_no_committed_job(stores: Stores, tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"original-file-bytes")
    ref = await stores.storage.upload(path=path, file_type=FileType.PDF)
    with pytest.raises(DependencyUnavailableError, match="upload_finalization_expired"):
        await stores.acceptance.accept(
            subject="owner",
            key="expired",
            upload=PreparedUpload(
                path=path, sha256="a" * 64, filename="a.pdf", file_type=FileType.PDF
            ),
            metadata=UploadMetadata(),
            pipeline=pipeline(),
            ref=ref,
            max_object_age_seconds=0,
            trace_context={},
        )
    async with stores.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM app.ingestion_jobs")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM app.upload_requests")) == 0
    assert await stores.storage.exists(ref=ref)


async def test_reconciler_waits_for_active_finalization_fence(
    stores: Stores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"original-bytes")
    ref = await stores.storage.upload(path=path, file_type=FileType.PDF)
    entered, release = asyncio.Event(), asyncio.Event()
    original_exists = MinioStorage.exists

    async def blocked_exists(self: MinioStorage, *, ref: ObjectRef) -> bool:
        entered.set()
        await release.wait()
        return await original_exists(self, ref=ref)

    monkeypatch.setattr(MinioStorage, "exists", blocked_exists)
    accepting = asyncio.create_task(
        stores.acceptance.accept(
            subject="owner",
            key="active-fence",
            upload=PreparedUpload(
                path=path, sha256="a" * 64, filename="a.pdf", file_type=FileType.PDF
            ),
            metadata=UploadMetadata(),
            pipeline=pipeline(),
            ref=ref,
            max_object_age_seconds=60,
            trace_context={},
        )
    )
    reconciler = OrphanReconciliation(
        engine=stores.engine, storage=stores.storage, grace_seconds=0, batch_size=100
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        cleanup = asyncio.create_task(reconciler.run_once())
        await asyncio.sleep(0.03)
        assert not cleanup.done()
        release.set()
        accepted = await accepting
        assert await cleanup == 0
        assert (
            await stores.status.job_status(subject="owner", job_id=accepted.job_id)
        ).status == "queued"
    finally:
        release.set()
        await asyncio.gather(accepting, return_exceptions=True)


async def test_expired_finalizer_cannot_attach_reconciled_object(
    stores: Stores, tmp_path: Path
) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"original-bytes")
    ref = await stores.storage.upload(path=path, file_type=FileType.PDF)
    assert (
        await OrphanReconciliation(
            engine=stores.engine, storage=stores.storage, grace_seconds=0, batch_size=100
        ).run_once()
        == 1
    )
    with pytest.raises(DependencyUnavailableError):
        await stores.acceptance.accept(
            subject="owner",
            key="removed-object",
            upload=PreparedUpload(
                path=path, sha256="a" * 64, filename="a.pdf", file_type=FileType.PDF
            ),
            metadata=UploadMetadata(),
            pipeline=pipeline(),
            ref=ref,
            max_object_age_seconds=60,
            trace_context={},
        )
    async with stores.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM app.ingestion_jobs")) == 0
