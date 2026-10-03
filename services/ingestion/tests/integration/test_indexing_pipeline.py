"""Upload to published evidence: selective resume, replacement, provider budgets and no-text."""

from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text

from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.domain.retrieval import RetrievalPolicy
from horizon_ingestion.application.process_job import process_job
from horizon_ingestion.domain.chunking import Extraction, TextUnit, build_manifest
from horizon_ingestion.domain.documents import ErrorCategory, FileType, Pipeline
from horizon_ingestion.ports.indexing import (
    EmbeddingRejectedError,
    EmbeddingUnavailableError,
)
from horizon_ingestion_testing.chat import quiet_telemetry
from horizon_ingestion_testing.files import pdf_bytes, word_bytes
from horizon_ingestion_testing.jobs import VECTOR
from horizon_ingestion_testing.pipeline import Harness, upload_pdf

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("filename", "mime", "content"),
    [
        pytest.param(
            "pages.pdf",
            "application/pdf",
            pdf_bytes(pages=("First page evidence", "Second page evidence")),
            id="pdf",
        ),
        pytest.param(
            "scope.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            word_bytes(),
            id="word",
        ),
    ],
)
async def test_upload_publishes_cited_private_evidence(
    harness: Harness, filename: str, mime: str, content: bytes
) -> None:
    response = await harness.client.post(
        "/v1/documents",
        files={"file": (filename, content, mime)},
        headers={"Idempotency-Key": "upload-one"},
    )
    assert response.status_code == 202
    accepted = response.json()
    claim = await harness.claim()
    query = PgvectorEvidenceIndex(
        engine=harness.engine,
        embedding_model_id="amazon.titan-embed-text-v2:0",
        telemetry=quiet_telemetry(),
    )
    policy = RetrievalPolicy(max_chunks=8, max_excerpt_chars=3000, max_evidence_chars=24000)
    assert await query.search(subject="owner", vector=VECTOR, k=5, policy=policy) == ()
    await harness.process(claim)
    status = await harness.client.get(accepted["status_url"])
    assert status.json()["status"] == "ready"
    assert status.json()["completed_chunks"] == status.json()["total_chunks"]
    hits = await query.search(subject="owner", vector=VECTOR, k=5, policy=policy)
    assert len(hits) == 1
    assert hits[0].source.filename == filename
    assert hits[0].source.document_version_id == UUID(accepted["version_id"])
    assert hits[0].source.end_offset is not None
    assert await query.search(subject="intruder", vector=VECTOR, k=5, policy=policy) == ()
    replay = await harness.client.post(
        "/v1/documents",
        files={"file": (filename, content, mime)},
        headers={"Idempotency-Key": "upload-one"},
    )
    assert replay.status_code == 200
    assert replay.json()["job_id"] == accepted["job_id"]


async def test_nul_bytes_in_extracted_pdf_text_are_removed_before_indexing(
    harness: Harness,
) -> None:
    response = await harness.client.post(
        "/v1/documents",
        files={
            "file": (
                "nul.pdf",
                pdf_bytes(pages=(r"Horizon\000evidence\001here",)),
                "application/pdf",
            )
        },
        headers={"Idempotency-Key": "nul-text"},
    )
    assert response.status_code == 202
    await harness.process(await harness.claim())
    status = await harness.client.get(response.json()["status_url"])
    assert status.json()["status"] == "ready"
    async with harness.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT text FROM app.document_chunks"))).scalars().all()
    assert stored == ["Horizonevidencehere"]


async def test_107_of_111_chunks_resume_without_reextracting(harness: Harness) -> None:
    result = await harness.client.post(
        "/v1/documents",
        files={"file": ("a.pdf", pdf_bytes(), "application/pdf")},
        headers={"Idempotency-Key": "selective"},
    )
    assert result.status_code == 202
    claim = await harness.queue.start_attempt(claim=await harness.claim())
    if claim.version_id is None:
        pytest.fail("index version missing")
    manifest = build_manifest(
        version_id=claim.version_id,
        extraction=Extraction(units=(TextUnit(text="a" * 200000, page=12),), title="Evidence"),
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=Pipeline(
            window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
        ),
    )
    await harness.index.save_manifest(claim=claim, chunks=manifest)
    for chunk in manifest[:4]:
        await harness.index.begin_chunk(claim=claim, chunk_id=chunk.id, max_attempts=9)
        await harness.index.complete_chunk(claim=claim, chunk_id=chunk.id, vector=VECTOR)
    initial = await harness.status.job_status(subject="owner", job_id=claim.job_id)
    assert initial.total_chunks == 111
    assert initial.completed_chunks == 4
    for chunk in manifest[4:107]:
        await harness.index.begin_chunk(claim=claim, chunk_id=chunk.id, max_attempts=9)
        await harness.index.complete_chunk(claim=claim, chunk_id=chunk.id, vector=VECTOR)
    await harness.queue.fail(claim=claim, category=ErrorCategory.PROVIDER, delay_seconds=None)
    progress = await harness.status.job_status(subject="owner", job_id=claim.job_id)
    assert progress.total_chunks == 111
    assert progress.completed_chunks == 107
    retried = await harness.client.post(f"/v1/ingestion-jobs/{claim.job_id}:retry")
    assert retried.status_code == 202
    assert retried.json()["filename"] == "a.pdf"
    next_claim = await harness.claim()
    await harness.process(next_claim)
    assert len(harness.embeddings.texts) == 4
    final = await harness.status.job_status(subject="owner", job_id=claim.job_id)
    assert final.status == "ready"
    assert final.completed_chunks == 111
    assert final.attempts == 2
    assert final.retry_cycle == 1


async def test_atomic_replacement_keeps_old_evidence_until_publication(harness: Harness) -> None:
    first = await upload_pdf(harness, key="initial", text_value="Old evidence")
    old = await harness.claim()
    await harness.process(old)
    query = PgvectorEvidenceIndex(
        engine=harness.engine,
        embedding_model_id="amazon.titan-embed-text-v2:0",
        telemetry=quiet_telemetry(),
    )
    policy = RetrievalPolicy(max_chunks=8, max_excerpt_chars=3000, max_evidence_chars=24000)
    original = (await query.search(subject="owner", vector=VECTOR, k=5, policy=policy))[0].source
    refs = await harness.storage.orphan_candidates(limit=100)
    second = await upload_pdf(
        harness, key="replacement", text_value="New evidence", document_id=old.document_id
    )
    assert second.status_code == 202
    assert second.json()["document_id"] == first.json()["document_id"]
    competing = await upload_pdf(
        harness, key="competing", text_value="Third evidence", document_id=old.document_id
    )
    assert competing.status_code == 409
    candidate = await harness.claim()
    assert (await query.search(subject="owner", vector=VECTOR, k=5, policy=policy))[
        0
    ].source == original
    await harness.process(candidate)
    hits = await query.search(subject="owner", vector=VECTOR, k=5, policy=policy)
    assert len(hits) == 1
    assert hits[0].source.document_version_id == candidate.version_id
    assert await harness.storage.exists(ref=refs[0])
    cleanup = await harness.claim()
    assert cleanup.kind == "superseded_cleanup"
    await harness.process(cleanup)
    assert not await harness.storage.exists(ref=refs[0])
    async with harness.engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM app.document_chunks WHERE version_id=:version"),
                {"version": old.version_id},
            )
            == 0
        )
    assert original.filename == "a.pdf"
    assert original.document_version_id == old.version_id


@dataclass(slots=True, kw_only=True)
class FailingEmbeddings:
    retryable: bool
    calls: int = 0

    async def embed(self, *, text: str) -> tuple[float, ...]:
        self.calls += 1
        if self.retryable:
            raise EmbeddingUnavailableError("provider_unavailable")
        raise EmbeddingRejectedError("provider_rejected")


@pytest.mark.parametrize(
    ("retryable", "calls", "state"), [(True, 3, "retrying"), (False, 1, "failed")]
)
async def test_finite_provider_retries_release_permits_during_backoff(
    harness: Harness, retryable: bool, calls: int, state: str
) -> None:
    await upload_pdf(harness, key="vendor-budget", text_value="Provider evidence")
    claim = await harness.claim()
    provider = FailingEmbeddings(retryable=retryable)

    async def backoff_probe(delay: float) -> None:
        async with harness.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM app.vendor_permits WHERE token IS NOT NULL")
                )
                == 0
            )

    await process_job(
        claim=claim, context=harness.context(embeddings=provider, sleep=backoff_probe)
    )
    assert provider.calls == calls
    assert (await harness.status.job_status(subject="owner", job_id=claim.job_id)).status == state
    if retryable:
        for _ in range(2):
            async with harness.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE app.ingestion_jobs SET next_retry_at=now()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": claim.job_id},
                )
            resumed = await harness.claim()
            await process_job(
                claim=resumed, context=harness.context(embeddings=provider, sleep=backoff_probe)
            )
        assert provider.calls == 9
        assert (
            await harness.status.job_status(subject="owner", job_id=claim.job_id)
        ).status == "failed"


async def test_image_only_pdf_finishes_with_explicit_no_text_failure(harness: Harness) -> None:
    response = await harness.client.post(
        "/v1/documents",
        files={"file": ("image.pdf", pdf_bytes(pages=("",)), "application/pdf")},
        headers={"Idempotency-Key": "image-only"},
    )
    assert response.status_code == 202
    claim = await harness.claim()
    await harness.process(claim)
    status = await harness.status.job_status(subject="owner", job_id=claim.job_id)
    assert status.status == "failed"
    assert status.error_category == ErrorCategory.NO_TEXT
    assert status.total_chunks is None
