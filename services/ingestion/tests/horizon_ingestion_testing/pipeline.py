"""Real database, storage and HTTP harness shared by the ingestion integration tests."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from mypy_boto3_s3 import S3Client
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_ingestion.adapters.documents import DocumentExtractor, DocumentPreparer
from horizon_ingestion.adapters.identity import LocalIdentityVerifier
from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.api.dependencies import ApiRuntime, get_runtime
from horizon_ingestion.application.job_context import JobContext, Sleep
from horizon_ingestion.application.process_job import process_job
from horizon_ingestion.bootstrap.app import create_app
from horizon_ingestion.bootstrap.worker import serve_jobs, worker_policy
from horizon_ingestion.config.settings import Settings
from horizon_ingestion.db.acceptance import PgAcceptanceStore
from horizon_ingestion.db.cleanup import PgCleanupStore
from horizon_ingestion.db.indexing import PgIndexStore
from horizon_ingestion.db.queue import PgWorkQueue
from horizon_ingestion.db.reconciliation import OrphanReconciliation
from horizon_ingestion.db.status import PgStatusStore
from horizon_ingestion.domain.documents import ExtractionPolicy, Pipeline, WorkerPolicy
from horizon_ingestion.observability.tracing import Measurements, Telemetry
from horizon_ingestion.ports.indexing import Claim, EmbeddingPort
from horizon_ingestion.workers.jobs import WorkerRuntime
from horizon_ingestion_testing.files import pdf_bytes
from horizon_ingestion_testing.jobs import VECTOR


@dataclass(slots=True)
class Embeddings:
    """Record provider input while replacing only the paid embedding boundary."""

    texts: list[str] = field(default_factory=list)

    async def embed(self, *, text: str) -> tuple[float, ...]:
        self.texts.append(text)
        return VECTOR


@dataclass(frozen=True, slots=True, kw_only=True)
class Harness:
    telemetry: Telemetry
    client: httpx.AsyncClient
    engine: AsyncEngine
    storage: MinioStorage
    queue: PgWorkQueue
    index: PgIndexStore
    cleanup: PgCleanupStore
    status: PgStatusStore
    embeddings: Embeddings
    extractor: DocumentExtractor
    policy: WorkerPolicy

    async def claim(self) -> Claim:
        claims = await self.queue.claim(worker_id="test-worker", limit=1, lease_seconds=90)
        if len(claims) != 1:
            raise RuntimeError("expected one due job")
        return claims[0]

    def context(
        self, *, embeddings: EmbeddingPort | None = None, sleep: Sleep = asyncio.sleep
    ) -> JobContext:
        return JobContext(
            index=self.index,
            cleanup=self.cleanup,
            queue=self.queue,
            storage=self.storage,
            extractor=self.extractor,
            embeddings=embeddings or self.embeddings,
            semaphore=asyncio.Semaphore(2),
            policy=self.policy,
            sleep=sleep,
            clock=lambda: datetime.now(UTC),
            id_factory=uuid4,
            jitter=random.uniform,
            telemetry=self.telemetry,
        )

    async def process(self, claim: Claim) -> None:
        await process_job(claim=claim, context=self.context())


@asynccontextmanager
async def open_harness(*, migrated_database: str, minio_client: S3Client) -> AsyncIterator[Harness]:
    """Real database, storage and HTTP app; only the paid embedding boundary is replaced."""
    settings = Settings(
        environment_name="local",
        bind_host="127.0.0.1",
        bind_port=8081,
        frontend_origin="http://localhost:3000",
        aws_region="eu-west-1",
        embedding_model_id="amazon.titan-embed-text-v2:0",
        google_client_id="public-client",
        minio_endpoint="http://localhost:9000",
        minio_bucket="horizon-documents",
    )
    engine = create_async_engine(
        migrated_database.replace("postgresql://", "postgresql+psycopg://")
    )
    storage = MinioStorage(
        client=minio_client,
        bucket="horizon-documents",
        prefix=f"attempts/{uuid4().hex}/",
        max_bytes=1000000,
    )
    extraction_policy = ExtractionPolicy(
        memory_bytes=536870912, max_chars=1000000, max_units=100, timeout_seconds=5
    )
    status = PgStatusStore(engine=engine)

    async def ready() -> bool:
        return True

    traces = TracerProvider()
    metrics = MeterProvider()
    telemetry = Telemetry(
        tracer=traces.get_tracer("test"), measurements=Measurements(meter=metrics.get_meter("test"))
    )
    runtime = ApiRuntime(
        telemetry=telemetry,
        check_readiness=ready,
        readiness_timeout_seconds=1,
        identity=LocalIdentityVerifier(subject="owner"),
        pipeline=Pipeline(
            window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
        ),
        preparer=DocumentPreparer(max_bytes=1000000, extraction_policy=extraction_policy),
        storage=storage,
        acceptance=PgAcceptanceStore(engine=engine, storage=storage),
        status=status,
        upload_timeout_seconds=30,
    )
    app = create_app(settings=settings)
    app.dependency_overrides[get_runtime] = lambda: runtime
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Harness(
                telemetry=telemetry,
                client=client,
                engine=engine,
                storage=storage,
                queue=PgWorkQueue(engine=engine),
                index=PgIndexStore(engine=engine),
                cleanup=PgCleanupStore(engine=engine),
                status=status,
                embeddings=Embeddings(),
                extractor=DocumentExtractor(policy=extraction_policy),
                policy=worker_policy(settings),
            )
    finally:
        for ref in await storage.orphan_candidates(limit=100):
            await storage.remove(ref=ref)
        await engine.dispose()
        traces.shutdown()
        metrics.shutdown()


async def upload_pdf(
    harness: Harness, *, key: str, text_value: str, document_id: UUID | None = None
) -> httpx.Response:
    data = {"document_id": str(document_id)} if document_id else {}
    return await harness.client.post(
        "/v1/documents",
        files={"file": ("a.pdf", pdf_bytes(pages=(text_value,)), "application/pdf")},
        data=data,
        headers={"Idempotency-Key": key},
    )


def reconciliation(harness: Harness, *, grace_seconds: float) -> OrphanReconciliation:
    """One page of orphan candidates per pass, as in production."""
    return OrphanReconciliation(
        engine=harness.engine, storage=harness.storage, grace_seconds=grace_seconds, batch_size=1
    )


async def serve(
    harness: Harness, *, runtime: WorkerRuntime, wakeup: asyncio.Event, stop: asyncio.Event
) -> None:
    """The production worker loops over the harness's database and storage."""
    await serve_jobs(
        runtime=runtime,
        reconciliation=reconciliation(
            harness, grace_seconds=runtime.job.policy.orphan_grace_seconds
        ),
        wakeup=wakeup,
        stop=stop,
    )
