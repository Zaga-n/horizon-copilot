"""Deletion fences active work, removes exact originals and survives storage outages."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text

from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.application.process_job import process_job
from horizon_ingestion.domain.documents import ErrorCategory
from horizon_ingestion.ports.errors import DependencyUnavailableError, RejectedError
from horizon_ingestion.ports.indexing import (
    StaleClaimError,
)
from horizon_ingestion.ports.uploads import ObjectRef
from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.jobs import VECTOR
from horizon_ingestion_testing.pipeline import Harness, upload_pdf

pytestmark = pytest.mark.integration
README = Path(__file__).resolve().parents[2] / "README.md"


def readme_query(name: str) -> str:
    """The operator query documented in the README under a `-- <name>` marker."""
    block = README.read_text().split(f"```sql\n-- {name}\n", 1)[1]
    return block.split("```", 1)[0]


async def test_deletion_fences_active_results_and_removes_exact_original(harness: Harness) -> None:
    result = await harness.client.post(
        "/v1/documents",
        files={"file": ("a.pdf", pdf_bytes(), "application/pdf")},
        headers={"Idempotency-Key": "delete-race"},
    )
    assert result.status_code == 202
    claim = await harness.claim()
    refs = await harness.storage.orphan_candidates(limit=100)
    assert len(refs) == 1
    removed = await harness.client.delete(f"/v1/documents/{claim.document_id}")
    assert removed.status_code == 202
    assert removed.json()["lifecycle"] == "deleting"
    with pytest.raises(StaleClaimError, match="lifecycle"):
        await harness.index.publish(claim=claim)
    pending = await harness.client.get(f"/v1/documents/{claim.document_id}")
    assert pending.json()["filename"] == "a.pdf"
    cleanup = await harness.claim()
    await harness.process(cleanup)
    assert not await harness.storage.exists(ref=refs[0])
    document = await harness.client.get(f"/v1/documents/{claim.document_id}")
    assert document.json()["lifecycle"] == "deleted"
    assert document.json()["filename"] is None
    assert (await harness.client.get(f"/v1/ingestion-jobs/{claim.job_id}")).json()[
        "filename"
    ] is None
    repeat = await harness.client.delete(f"/v1/documents/{claim.document_id}")
    assert repeat.status_code == 200
    async with harness.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM app.document_chunks")) == 0
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM app.document_versions WHERE object_key IS NOT NULL")
            )
            == 0
        )
    reupload = await harness.client.post(
        "/v1/documents",
        files={"file": ("a.pdf", pdf_bytes(), "application/pdf")},
        headers={"Idempotency-Key": "fresh-lifecycle"},
    )
    assert reupload.status_code == 202
    assert reupload.json()["document_id"] != str(claim.document_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockingEmbeddings:
    entered: asyncio.Event
    release: asyncio.Event

    async def embed(self, *, text: str) -> tuple[float, ...]:
        self.entered.set()
        await self.release.wait()
        return VECTOR


async def test_delete_during_provider_call_cannot_restore_evidence(harness: Harness) -> None:
    await upload_pdf(harness, key="physical-race", text_value="Race evidence")
    claim = await harness.claim()
    entered, release = asyncio.Event(), asyncio.Event()
    work = asyncio.create_task(
        process_job(
            claim=claim,
            context=harness.context(
                embeddings=BlockingEmbeddings(entered=entered, release=release)
            ),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        response = await harness.client.delete(f"/v1/documents/{claim.document_id}")
        assert response.status_code == 202
        retry = await harness.client.post(f"/v1/ingestion-jobs/{claim.job_id}:retry")
        assert retry.status_code == 409
        release.set()
        await work  # the fenced attempt stops without settling anything
        await harness.process(await harness.claim())
        async with harness.engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM app.document_chunks")) == 0
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


async def test_storage_outage_keeps_deletion_durable_until_recovery(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await upload_pdf(harness, key="cleanup-recovery", text_value="Cleanup evidence")
    index_claim = await harness.claim()
    await harness.process(index_claim)
    await harness.client.delete(f"/v1/documents/{index_claim.document_id}")
    cleanup = await harness.claim()
    remove = MinioStorage.remove

    async def unavailable(self: MinioStorage, *, ref: ObjectRef) -> None:
        raise DependencyUnavailableError("storage_unavailable")

    monkeypatch.setattr(MinioStorage, "remove", unavailable)
    await harness.process(cleanup)
    assert (
        await harness.status.document_status(subject="owner", document_id=index_claim.document_id)
    ).lifecycle == "deleting"
    async with harness.engine.begin() as conn:
        assert (
            await conn.scalar(
                text("SELECT status FROM app.ingestion_jobs WHERE id=:id"), {"id": cleanup.job_id}
            )
            == "retrying"
        )
        await conn.execute(
            text("UPDATE app.ingestion_jobs SET next_retry_at=now() WHERE id=:id"),
            {"id": cleanup.job_id},
        )
    monkeypatch.setattr(MinioStorage, "remove", remove)
    await harness.process(await harness.claim())
    assert (
        await harness.status.document_status(subject="owner", document_id=index_claim.document_id)
    ).lifecycle == "deleted"


async def test_repeated_delete_resumes_failed_cleanup_without_allocating_another_job(
    harness: Harness,
) -> None:
    uploaded = await upload_pdf(harness, key="failed-cleanup", text_value="Evidence")
    document_id = uploaded.json()["document_id"]
    await harness.client.delete(f"/v1/documents/{document_id}")
    cleanup = await harness.queue.start_attempt(claim=await harness.claim())
    await harness.queue.fail(claim=cleanup, category=ErrorCategory.STORAGE, delay_seconds=None)
    repeated = await harness.client.delete(f"/v1/documents/{document_id}")
    assert repeated.status_code == 202
    resumed = await harness.status.document_status(subject="owner", document_id=cleanup.document_id)
    assert resumed.job_id == cleanup.job_id
    assert resumed.status == "queued"
    assert resumed.retry_cycle == 1
    assert resumed.attempts == 1
    await harness.process(await harness.claim())
    deleted = await harness.client.get(f"/v1/documents/{document_id}")
    assert deleted.json()["lifecycle"] == "deleted"
    assert deleted.json()["filename"] is None
    async with harness.engine.connect() as conn:
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM app.ingestion_jobs WHERE document_id=:id AND kind='delete'"
                ),
                {"id": cleanup.document_id},
            )
            == 1
        )


async def test_a_permanent_cleanup_fault_fails_and_a_repeated_delete_requeues_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded = await upload_pdf(harness, key="permanent-cleanup", text_value="Evidence")
    document_id = uploaded.json()["document_id"]
    await harness.process(await harness.claim())
    await harness.client.delete(f"/v1/documents/{document_id}")
    remove = MinioStorage.remove

    async def refused(self: MinioStorage, *, ref: ObjectRef) -> None:
        raise RejectedError("storage_rejected")

    monkeypatch.setattr(MinioStorage, "remove", refused)
    claims = []
    for _ in range(harness.policy.max_job_attempts):
        claims.append(await harness.claim())
        await harness.process(claims[-1])
        async with harness.engine.begin() as conn:
            await conn.execute(
                text("UPDATE app.ingestion_jobs SET next_retry_at=now() WHERE id=:id"),
                {"id": claims[-1].job_id},
            )
    cleanup = claims[-1]
    assert {claim.job_id for claim in claims} == {cleanup.job_id}
    failed = await harness.status.document_status(subject="owner", document_id=cleanup.document_id)
    assert (failed.status, failed.lifecycle) == ("failed", "deleting")
    assert failed.error_category == ErrorCategory.INTEGRITY
    assert await harness.queue.claim(worker_id="test-worker", limit=1, lease_seconds=90) == ()
    async with harness.engine.connect() as conn:
        stuck = (await conn.execute(text(readme_query("stuck-deletions")))).mappings().all()
    assert [(row["document_id"], row["job_id"]) for row in stuck] == [
        (cleanup.document_id, cleanup.job_id)
    ]
    monkeypatch.setattr(MinioStorage, "remove", remove)
    repeated = await harness.client.delete(f"/v1/documents/{document_id}")
    assert repeated.status_code == 202
    await harness.process(await harness.claim())
    deleted = await harness.client.get(f"/v1/documents/{document_id}")
    assert deleted.json()["lifecycle"] == "deleted"
