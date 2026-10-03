"""Control characters in upload names and metadata are rejected before any original is stored."""

import asyncio
import json

import pytest

from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.pipeline import Harness

pytestmark = pytest.mark.integration


BOUNDARY = "horizon-test-boundary"


def multipart(*, filename: str, metadata: dict[str, object]) -> bytes:
    """Raw form body: HTTP clients percent-encode control bytes in filenames, browsers may not."""
    parts = [
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
        + json.dumps(metadata).encode()
        + b"\r\n",
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
        "\r\nContent-Type: application/pdf\r\n\r\n".encode()
        + pdf_bytes()
        + b"\r\n",
    ]
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


async def stored_versions(harness: Harness) -> int:
    response = await asyncio.to_thread(
        harness.storage.client.list_object_versions,
        Bucket=harness.storage.bucket,
        Prefix=harness.storage.prefix,
    )
    return len(response.get("Versions", []))


@pytest.mark.parametrize(
    ("filename", "metadata", "code"),
    [
        pytest.param("plan\x00.pdf", {}, "invalid_filename", id="nul-filename"),
        pytest.param("plan\x1b.pdf", {}, "invalid_filename", id="escape-filename"),
        pytest.param("plan.pdf", {"corpus": "wp\x00"}, "invalid_upload_metadata", id="nul-corpus"),
        pytest.param(
            "plan.pdf",
            {"project_metadata": {"owner": "a\x00b"}},
            "invalid_upload_metadata",
            id="nul-metadata-value",
        ),
        pytest.param(
            "plan.pdf",
            {"project_metadata": {"own\ner": "a"}},
            "invalid_upload_metadata",
            id="newline-metadata-key",
        ),
    ],
)
async def test_control_characters_are_rejected_before_storage(
    harness: Harness, filename: str, metadata: dict[str, object], code: str
) -> None:
    before = await stored_versions(harness)
    response = await harness.client.post(
        "/v1/documents",
        content=multipart(filename=filename, metadata=metadata),
        headers={
            "Idempotency-Key": "control-characters",
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": code}
    assert await stored_versions(harness) == before


async def test_tab_and_newline_are_allowed_in_free_text_metadata_values(harness: Harness) -> None:
    response = await harness.client.post(
        "/v1/documents",
        files={"file": ("plan.pdf", pdf_bytes(), "application/pdf")},
        data={"metadata": json.dumps({"project_metadata": {"notes": "line one\n\tline two"}})},
        headers={"Idempotency-Key": "free-text"},
    )
    assert response.status_code == 202
