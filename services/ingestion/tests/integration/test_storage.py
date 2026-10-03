"""Exercise immutable originals and bucket-scoped permissions against disposable MinIO."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from mypy_boto3_s3 import S3Client

from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.domain.documents import FileType

pytestmark = pytest.mark.integration


async def test_exact_original_version_is_removed(minio_client: S3Client, tmp_path: Path) -> None:
    storage = MinioStorage(
        client=minio_client,
        bucket="horizon-documents",
        prefix=f"attempts/{uuid4().hex}/",
        max_bytes=100,
    )
    path = tmp_path / "test.pdf"
    path.write_bytes(b"original-file-bytes")
    ref = await storage.upload(path=path, file_type=FileType.PDF)
    try:
        assert await storage.exists(ref=ref)
        copy = tmp_path / "download.pdf"
        await storage.download(ref=ref, path=copy)
        assert copy.read_bytes() == b"original-file-bytes"
        await storage.remove(ref=ref)
        assert not await storage.exists(ref=ref)
        await storage.remove(ref=ref)
        versions = await storage.orphan_candidates(limit=100)
        assert not versions
    finally:
        await storage.remove(ref=ref)


async def test_bucket_role_cannot_access_unowned_storage(minio_client: S3Client) -> None:
    visible = await asyncio.to_thread(minio_client.list_buckets)
    assert [bucket["Name"] for bucket in visible.get("Buckets", ())] == ["horizon-documents"]
    with pytest.raises(ClientError, match="AccessDenied"):
        await asyncio.to_thread(
            minio_client.get_object, Bucket="another-private-bucket", Key="secret"
        )
    with pytest.raises(ClientError, match="AccessDenied"):
        await asyncio.to_thread(
            minio_client.put_object,
            Bucket="horizon-documents",
            Key="outside-owned-prefix",
            Body=b"x",
        )
    with pytest.raises(ClientError, match="AccessDenied"):
        await asyncio.to_thread(
            minio_client.put_bucket_versioning,
            Bucket="horizon-documents",
            VersioningConfiguration={"Status": "Suspended"},
        )
