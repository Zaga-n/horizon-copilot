"""Lifecycle responses project canonical filenames without preserving deleted metadata."""

from uuid import UUID

import pytest
from sqlalchemy import text

from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.pipeline import Harness, upload_pdf

pytestmark = pytest.mark.integration


async def test_reloaded_status_uses_canonical_name_after_content_deduplication(
    harness: Harness,
) -> None:
    original = await upload_pdf(harness, key="original", text_value="Canonical evidence")
    accepted = original.json()
    duplicate = await harness.client.post(
        "/v1/documents",
        files={
            "file": ("renamed.pdf", pdf_bytes(pages=("Canonical evidence",)), "application/pdf")
        },
        headers={"Idempotency-Key": "duplicate"},
    )
    assert duplicate.json()["document_id"] == accepted["document_id"]
    assert duplicate.json()["deduplicated"] is True
    for path in (accepted["status_url"], f"/v1/documents/{accepted['document_id']}"):
        status = await harness.client.get(path)
        assert status.status_code == 200
        assert status.json()["filename"] == "a.pdf"
    listed = await harness.client.get("/v1/documents")
    assert [item["filename"] for item in listed.json()] == ["a.pdf"]


async def test_index_status_names_candidate_while_delete_status_names_document(
    harness: Harness,
) -> None:
    original = await upload_pdf(harness, key="initial", text_value="Published evidence")
    document_id = UUID(original.json()["document_id"])
    await harness.process(await harness.claim())
    replacement = await harness.client.post(
        "/v1/documents",
        files={"file": ("replacement.pdf", pdf_bytes(pages=("New evidence",)), "application/pdf")},
        data={"document_id": str(document_id)},
        headers={"Idempotency-Key": "replacement"},
    )
    assert replacement.status_code == 202
    status = await harness.client.get(f"/v1/documents/{document_id}")
    assert status.json()["filename"] == "replacement.pdf"
    await harness.client.delete(f"/v1/documents/{document_id}")
    deleting = await harness.client.get(f"/v1/documents/{document_id}")
    assert deleting.json()["lifecycle"] == "deleting"
    assert deleting.json()["filename"] == "a.pdf"
    await harness.process(await harness.claim())
    deleted = await harness.client.get(f"/v1/documents/{document_id}")
    assert deleted.json()["lifecycle"] == "deleted"
    assert deleted.json()["filename"] is None
    assert (await harness.client.get(replacement.json()["status_url"])).json()["filename"] is None


async def test_one_corrupt_status_row_does_not_fail_the_listing(harness: Harness) -> None:
    healthy = (await upload_pdf(harness, key="healthy", text_value="Healthy evidence")).json()
    corrupt = (await upload_pdf(harness, key="corrupt", text_value="Corrupt evidence")).json()
    async with harness.engine.begin() as conn:
        await conn.execute(
            text("UPDATE app.ingestion_jobs SET error_category='not-a-category' WHERE id=:id"),
            {"id": corrupt["job_id"]},
        )
    listing = await harness.client.get("/v1/documents")
    assert listing.status_code == 200
    by_job = {item["job_id"]: item for item in listing.json()}
    assert by_job[healthy["job_id"]]["status"] == "queued"
    assert (by_job[corrupt["job_id"]]["status"], by_job[corrupt["job_id"]]["error_category"]) == (
        "failed",
        "integrity_failed",
    )
    assert (await harness.client.get(corrupt["status_url"])).status_code == 500
