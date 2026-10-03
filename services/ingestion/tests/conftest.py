"""Disposable object-storage fixtures shared by ingestion integration tests."""

import os
from collections.abc import AsyncIterator, Iterator

import boto3
import pytest
from botocore.config import Config
from mypy_boto3_s3 import S3Client

from horizon_ingestion_testing.pipeline import Harness, open_harness


@pytest.fixture
def minio_client() -> Iterator[S3Client]:
    endpoint = os.environ.get("TEST_MINIO_ENDPOINT")
    if not endpoint:
        if os.environ.get("REQUIRE_INTEGRATION") == "1":
            pytest.fail("TEST_MINIO_ENDPOINT is required by the integration profile")
        pytest.skip("Set TEST_MINIO_ENDPOINT to disposable MinIO provisioned with dev/stack/minio")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test-ingestion",
        aws_secret_access_key="disposable-ingestion-password",
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=1, read_timeout=1),
    )
    yield client
    client.close()


@pytest.fixture
async def harness(migrated_database: str, minio_client: S3Client) -> AsyncIterator[Harness]:
    async with open_harness(
        migrated_database=migrated_database, minio_client=minio_client
    ) as opened:
        yield opened
