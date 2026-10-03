"""Own ingestion connection pools and release resources on shutdown or startup failure."""

from collections.abc import AsyncIterator, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)

import boto3
import httpx
from botocore.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_google_identity import GoogleIdTokenVerifier, HttpCertificateRequest
from horizon_ingestion.adapters.documents import DocumentPreparer
from horizon_ingestion.adapters.identity import GoogleIdentityVerifier, LocalIdentityVerifier
from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.api.dependencies import ApiRuntime
from horizon_ingestion.config.secrets import Secrets
from horizon_ingestion.config.settings import Settings
from horizon_ingestion.db.acceptance import PgAcceptanceStore
from horizon_ingestion.db.readiness import Readiness
from horizon_ingestion.db.status import PgStatusStore
from horizon_ingestion.domain.documents import ExtractionPolicy, Pipeline
from horizon_ingestion.observability.tracing import Telemetry, open_telemetry
from horizon_ingestion.ports.identity import IdentityVerifier


@asynccontextmanager
async def build_runtime(
    settings: Settings, secrets: Secrets, *, engine: AsyncEngine | None = None
) -> AsyncIterator[ApiRuntime]:
    async with AsyncExitStack() as stack:
        telemetry = await stack.enter_async_context(open_service_telemetry(settings))
        if engine is None:
            engine = await stack.enter_async_context(
                open_database(settings=settings, secrets=secrets)
            )
        storage = stack.enter_context(open_storage(settings=settings, secrets=secrets))
        yield ApiRuntime(
            check_readiness=readiness(settings=settings, engine=engine).check,
            readiness_timeout_seconds=settings.readiness_timeout_seconds,
            telemetry=telemetry,
            identity=_build_identity(settings=settings, stack=stack),
            pipeline=Pipeline(
                window_size=settings.window_size,
                overlap=settings.overlap,
                embedding_model_id=settings.embedding_model_id,
            ),
            preparer=DocumentPreparer(
                max_bytes=settings.max_upload_bytes, extraction_policy=extraction_policy(settings)
            ),
            storage=storage,
            acceptance=PgAcceptanceStore(engine=engine, storage=storage),
            status=PgStatusStore(engine=engine),
            upload_timeout_seconds=settings.upload_timeout_seconds,
        )


def _build_identity(*, settings: Settings, stack: AsyncExitStack) -> IdentityVerifier:
    if settings.identity_mode == "local":
        return LocalIdentityVerifier(subject=settings.local_subject)
    http = stack.enter_context(
        httpx.Client(
            timeout=httpx.Timeout(
                settings.provider_read_timeout_seconds,
                connect=settings.provider_connect_timeout_seconds,
            )
        )
    )
    return GoogleIdentityVerifier(
        verifier=GoogleIdTokenVerifier(
            audience=settings.google_client_id, request=HttpCertificateRequest(client=http)
        )
    )


def open_service_telemetry(settings: Settings) -> AbstractAsyncContextManager[Telemetry]:
    return open_telemetry(
        environment=settings.environment_name,
        endpoint=str(settings.otlp_endpoint) if settings.otlp_endpoint else None,
        instance_id=settings.service_instance_id,
    )


def readiness(*, settings: Settings, engine: AsyncEngine) -> Readiness:
    return Readiness(engine=engine, embedding_dimensions=settings.embedding_dimensions)


def extraction_policy(settings: Settings) -> ExtractionPolicy:
    return ExtractionPolicy(
        memory_bytes=settings.parser_memory_bytes,
        max_chars=settings.max_extracted_chars,
        max_units=settings.max_extraction_units,
        timeout_seconds=settings.extraction_timeout_seconds,
    )


@contextmanager
def open_storage(*, settings: Settings, secrets: Secrets) -> Iterator[MinioStorage]:
    """The bucket adapter over an S3 client this context owns and closes."""
    client = boto3.client(
        "s3",
        endpoint_url=str(settings.minio_endpoint),
        region_name=settings.aws_region,
        aws_access_key_id=secrets.minio_access_key.get_secret_value(),
        aws_secret_access_key=secrets.minio_secret_key.get_secret_value(),
        config=Config(
            connect_timeout=settings.provider_connect_timeout_seconds,
            read_timeout=settings.provider_read_timeout_seconds,
            retries={"total_max_attempts": 1},
        ),
    )
    try:
        yield MinioStorage(
            client=client,
            bucket=settings.minio_bucket,
            prefix=settings.object_prefix,
            max_bytes=settings.max_upload_bytes,
        )
    finally:
        client.close()


@asynccontextmanager
async def open_database(*, settings: Settings, secrets: Secrets) -> AsyncIterator[AsyncEngine]:
    url = make_url(secrets.database_dsn.get_secret_value()).set(drivername="postgresql+psycopg")
    engine = create_async_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.database_connect_timeout_seconds,
            "options": f"-c statement_timeout={settings.database_statement_timeout_ms}",
        },
    )
    try:
        yield engine
    finally:
        await engine.dispose()
