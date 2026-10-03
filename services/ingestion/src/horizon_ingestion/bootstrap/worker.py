"""Worker process resource and signal ownership; supervised job and maintenance loops."""

import asyncio
import random
import signal
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_genai import BedrockConnection, bedrock_runtime_client
from horizon_ingestion.adapters.documents import DocumentExtractor
from horizon_ingestion.adapters.storage import MinioStorage
from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.bootstrap.runtime import (
    extraction_policy,
    open_database,
    open_service_telemetry,
    open_storage,
    readiness,
)
from horizon_ingestion.bootstrap.supervisor import LoopPolicy, ProcessHealth, run_supervised
from horizon_ingestion.bootstrap.worker_health import WorkerHealth, publish_health
from horizon_ingestion.config.secrets import Secrets, load_secrets
from horizon_ingestion.config.settings import Settings
from horizon_ingestion.db.cleanup import PgCleanupStore
from horizon_ingestion.db.indexing import PgIndexStore
from horizon_ingestion.db.queue import JobListener, PgWorkQueue
from horizon_ingestion.db.reconciliation import OrphanReconciliation
from horizon_ingestion.domain.documents import WorkerPolicy
from horizon_ingestion.genai.embeddings import TitanEmbeddings, build_document_embeddings
from horizon_ingestion.observability.tracing import Telemetry
from horizon_ingestion.workers.jobs import JobDispatcher, WorkerRuntime
from horizon_ingestion.workers.reconciliation import ReconciliationJob, reconciliation_iteration


class WorkerLoopCrashedError(Exception):
    """A required worker loop stopped unexpectedly; the process exits to be restarted."""


async def run_worker(*, settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        secrets = load_secrets()
        async with AsyncExitStack() as stack:
            telemetry = await stack.enter_async_context(open_service_telemetry(settings))
            engine = await stack.enter_async_context(
                open_database(settings=settings, secrets=secrets)
            )
            async with asyncio.timeout(settings.readiness_timeout_seconds):
                if not await readiness(settings=settings, engine=engine).check():
                    raise RuntimeError("Ingestion requires the current application migration")
            storage = stack.enter_context(open_storage(settings=settings, secrets=secrets))
            # Bootstrap owns the runtime client: it is closed with the process's other resources,
            # and botocore construction is CPU-bound, so it runs off the event loop here.
            bedrock = await asyncio.to_thread(
                bedrock_runtime_client, bedrock_connection(settings, secrets)
            )
            stack.callback(bedrock.close)
            worker = WorkerRuntime(
                job=_job_context(
                    settings=settings,
                    engine=engine,
                    storage=storage,
                    embeddings=build_document_embeddings(
                        client=bedrock,
                        model_id=settings.embedding_model_id,
                        dimensions=settings.embedding_dimensions,
                        telemetry=telemetry,
                    ),
                    telemetry=telemetry,
                ),
                telemetry=telemetry,
                worker_id=settings.service_instance_id or str(uuid4()),
            )
            listener = JobListener(
                dsn=secrets.database_dsn.get_secret_value().replace(
                    "postgresql+psycopg://", "postgresql://"
                ),
                connect_timeout_seconds=settings.database_connect_timeout_seconds,
                scan_interval_seconds=settings.scan_interval_seconds,
                reconnect_max_seconds=settings.retry_max_backoff_seconds,
            )
            wakeup = asyncio.Event()
            connected = asyncio.Event()
            listener_task = asyncio.create_task(
                run_supervised(
                    name="job-listener",
                    iteration=partial(
                        listener.listen, wakeup=wakeup, stop=stop, connected=connected
                    ),
                    policy=LoopPolicy(
                        interval_seconds=settings.scan_interval_seconds,
                        max_backoff_seconds=settings.retry_max_backoff_seconds,
                    ),
                    stop=stop,
                    health=ProcessHealth(),
                    required=False,
                )
            )
            try:
                async with asyncio.timeout(settings.readiness_timeout_seconds):
                    await wakeup.wait()
                await serve_jobs(
                    runtime=worker,
                    reconciliation=OrphanReconciliation(
                        engine=engine,
                        storage=storage,
                        grace_seconds=settings.orphan_grace_seconds,
                        batch_size=settings.maintenance_batch_size,
                    ),
                    wakeup=wakeup,
                    stop=stop,
                    health=WorkerHealth(
                        loops=ProcessHealth(),
                        listener_connected=connected,
                        max_scan_age_seconds=settings.scan_interval_seconds
                        + settings.retry_max_backoff_seconds
                        + settings.database_connect_timeout_seconds
                        + settings.database_statement_timeout_ms / 1000,
                    ),
                )
            finally:
                stop.set()
                listener_task.cancel()
                await asyncio.gather(listener_task, return_exceptions=True)
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


async def serve_jobs(
    *,
    runtime: WorkerRuntime,
    reconciliation: ReconciliationJob,
    wakeup: asyncio.Event,
    stop: asyncio.Event,
    health: WorkerHealth | None = None,
) -> None:
    """Claim and reconcile until `stop`, then drain running claims within the grace period."""
    policy = runtime.job.policy
    process_health = health.loops if health is not None else ProcessHealth()
    dispatcher = JobDispatcher(runtime=runtime, wakeup=wakeup)
    try:
        async with asyncio.TaskGroup() as tasks:
            if health is not None:
                tasks.create_task(
                    publish_health(
                        health=health, stop=stop, interval_seconds=policy.scan_interval_seconds
                    )
                )
            tasks.create_task(
                run_supervised(
                    name="job-dispatch",
                    iteration=dispatcher.claim_due,
                    policy=LoopPolicy(
                        interval_seconds=policy.scan_interval_seconds,
                        max_backoff_seconds=policy.retry_max_backoff_seconds,
                    ),
                    stop=stop,
                    health=process_health,
                    wakeup=wakeup,
                )
            )
            tasks.create_task(
                run_supervised(
                    name="orphan-reconciliation",
                    iteration=partial(
                        reconciliation_iteration, job=reconciliation, telemetry=runtime.telemetry
                    ),
                    policy=LoopPolicy(
                        interval_seconds=policy.reconciliation_interval_seconds,
                        max_backoff_seconds=policy.reconciliation_interval_seconds,
                    ),
                    stop=stop,
                    health=process_health,
                )
            )
    finally:
        await dispatcher.drain(grace_seconds=policy.shutdown_grace_seconds)
    if not process_health.loops_alive():
        raise WorkerLoopCrashedError(", ".join(sorted(process_health.stopped)))


def _job_context(
    *,
    settings: Settings,
    engine: AsyncEngine,
    storage: MinioStorage,
    embeddings: TitanEmbeddings,
    telemetry: Telemetry,
) -> JobContext:
    return JobContext(
        queue=PgWorkQueue(engine=engine),
        index=PgIndexStore(engine=engine),
        cleanup=PgCleanupStore(engine=engine),
        storage=storage,
        extractor=DocumentExtractor(policy=extraction_policy(settings)),
        embeddings=embeddings,
        semaphore=asyncio.Semaphore(settings.vendor_concurrency),
        policy=worker_policy(settings),
        sleep=asyncio.sleep,
        clock=lambda: datetime.now(UTC),
        id_factory=uuid4,
        jitter=random.uniform,
        telemetry=telemetry,
    )


def bedrock_connection(settings: Settings, secrets: Secrets) -> BedrockConnection:
    return BedrockConnection(
        region=settings.aws_region,
        connect_timeout_seconds=settings.provider_connect_timeout_seconds,
        read_timeout_seconds=settings.provider_read_timeout_seconds,
        aws_access_key_id=secrets.aws_access_key_id,
        aws_secret_access_key=secrets.aws_secret_access_key,
        aws_session_token=secrets.aws_session_token,
    )


def worker_policy(settings: Settings) -> WorkerPolicy:
    return WorkerPolicy(
        concurrency=settings.worker_concurrency,
        vendor_concurrency=settings.vendor_concurrency,
        chunk_timeout_seconds=settings.chunk_timeout_seconds,
        job_timeout_seconds=settings.job_timeout_seconds,
        scan_interval_seconds=settings.scan_interval_seconds,
        lease_seconds=settings.job_lease_seconds,
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        permit_lease_seconds=settings.permit_lease_seconds,
        max_call_attempts=settings.max_call_attempts,
        max_chunk_attempts=settings.max_chunk_attempts,
        max_job_attempts=settings.max_job_attempts,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        retry_initial_backoff_seconds=settings.retry_initial_backoff_seconds,
        retry_max_backoff_seconds=settings.retry_max_backoff_seconds,
        reconciliation_interval_seconds=settings.reconciliation_interval_seconds,
        orphan_grace_seconds=settings.orphan_grace_seconds,
        maintenance_batch_size=settings.maintenance_batch_size,
    )
