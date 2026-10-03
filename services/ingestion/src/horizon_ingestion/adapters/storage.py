"""Bucket-scoped immutable S3 originals; physical SDK attempts have SDK retries disabled."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

from horizon_ingestion.domain.documents import FileType
from horizon_ingestion.ports.errors import DependencyUnavailableError
from horizon_ingestion.ports.uploads import ObjectRef


@dataclass(slots=True)
class ListingCursor:
    key: str = ""
    version: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class MinioStorage:
    """Borrow the client; exact version deletes remove bytes rather than adding delete markers."""

    client: "S3Client"
    bucket: str
    prefix: str
    max_bytes: int
    cursor: ListingCursor = field(default_factory=ListingCursor)
    listing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def upload(self, *, path: Path, file_type: FileType) -> ObjectRef:
        key = f"{self.prefix}{uuid4().hex}.{file_type}"
        try:
            response = await asyncio.to_thread(self._put, path, key)
        except (BotoCoreError, ClientError) as exc:
            raise DependencyUnavailableError("storage_unavailable") from exc
        version = response
        if not version or version == "null":
            raise DependencyUnavailableError("versioned_bucket_required")
        return ObjectRef(key=key, version_id=version, created_at=datetime.now(UTC))

    def _put(self, path: Path, key: str) -> str:
        with path.open("rb") as source:
            response = self.client.put_object(
                Bucket=self.bucket, Key=key, Body=source, ContentLength=path.stat().st_size
            )
        return response.get("VersionId", "")

    async def exists(self, *, ref: ObjectRef) -> bool:
        try:
            await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=ref.key, VersionId=ref.version_id
            )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return False
            raise DependencyUnavailableError("storage_unavailable") from exc
        except BotoCoreError as exc:
            raise DependencyUnavailableError("storage_unavailable") from exc
        return True

    async def remove(self, *, ref: ObjectRef) -> None:
        try:
            await asyncio.to_thread(
                self.client.delete_object, Bucket=self.bucket, Key=ref.key, VersionId=ref.version_id
            )
        except (BotoCoreError, ClientError) as exc:
            raise DependencyUnavailableError("storage_unavailable") from exc

    async def download(self, *, ref: ObjectRef, path: Path) -> None:
        try:
            await asyncio.to_thread(self._download, ref, path)
        except (BotoCoreError, ClientError) as exc:
            raise DependencyUnavailableError("storage_unavailable") from exc

    def _download(self, ref: ObjectRef, path: Path) -> None:
        response = self.client.get_object(Bucket=self.bucket, Key=ref.key, VersionId=ref.version_id)
        body = response["Body"]
        try:
            if response.get("ContentLength", self.max_bytes + 1) > self.max_bytes:
                raise DependencyUnavailableError("storage_size_mismatch")
            with path.open("wb") as output:
                total = 0
                while block := body.read(64 * 1024):
                    total += len(block)
                    if total > self.max_bytes:
                        raise DependencyUnavailableError("storage_size_mismatch")
                    output.write(block)
        finally:
            body.close()

    async def orphan_candidates(self, *, limit: int) -> tuple[ObjectRef, ...]:
        # Advance one bounded page per scan; restart scans safely from the beginning.
        async with self.listing_lock:
            try:
                response = await asyncio.to_thread(
                    self.client.list_object_versions,
                    Bucket=self.bucket,
                    Prefix=self.prefix,
                    MaxKeys=limit,
                    KeyMarker=self.cursor.key,
                    VersionIdMarker=self.cursor.version,
                )
            except (BotoCoreError, ClientError) as exc:
                raise DependencyUnavailableError("storage_unavailable") from exc
            self.cursor.key = (
                response.get("NextKeyMarker", "") if response.get("IsTruncated") else ""
            )
            self.cursor.version = response.get("NextVersionIdMarker", "") if self.cursor.key else ""
            return tuple(
                ObjectRef(
                    key=version["Key"],
                    version_id=version["VersionId"],
                    created_at=version["LastModified"],
                )
                for version in response.get("Versions", ())
            )
