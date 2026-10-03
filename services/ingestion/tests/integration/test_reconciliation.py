"""Orphan reconciliation is age-bounded and pages past referenced originals."""

from pathlib import Path

import pytest

from horizon_ingestion.domain.documents import FileType
from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.pipeline import Harness, reconciliation, upload_pdf

pytestmark = pytest.mark.integration


async def test_reconciliation_is_age_bounded_and_advances_past_referenced_objects(
    harness: Harness, tmp_path: Path
) -> None:
    await upload_pdf(harness, key="referenced", text_value="Retain original")
    path = tmp_path / "orphan.pdf"
    path.write_bytes(pdf_bytes())
    orphan = await harness.storage.upload(path=path, file_type=FileType.PDF)
    assert await reconciliation(harness, grace_seconds=600).run_once() == 0
    removed = 0
    for _ in range(4):
        removed += await reconciliation(harness, grace_seconds=0).run_once()
    assert removed == 1
    assert not await harness.storage.exists(ref=orphan)
    claim = await harness.claim()
    await harness.process(claim)
    assert (await harness.status.job_status(subject="owner", job_id=claim.job_id)).status == "ready"
