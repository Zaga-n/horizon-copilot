"""In-memory queue, index and storage doubles recording what one job attempt settled."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from opentelemetry.metrics import NoOpMeter
from opentelemetry.trace import NoOpTracer

from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.domain.chunking import Chunk, Extraction
from horizon_ingestion.domain.documents import (
    ErrorCategory,
    FileType,
    JobKind,
    Pipeline,
    WorkerPolicy,
)
from horizon_ingestion.observability.tracing import Measurements, Telemetry
from horizon_ingestion.ports.indexing import ChunkWork, Claim, EmbeddingPort, VersionWork
from horizon_ingestion.ports.uploads import ObjectRef

NOW = datetime(2026, 10, 2, tzinfo=UTC)
VECTOR = (1.0,) + (0.0,) * 1023


POLICY = WorkerPolicy(
    chunk_timeout_seconds=5,
    job_timeout_seconds=30,
    concurrency=2,
    vendor_concurrency=2,
    scan_interval_seconds=0.01,
    lease_seconds=90,
    heartbeat_interval_seconds=20,
    permit_lease_seconds=45,
    max_call_attempts=3,
    max_chunk_attempts=9,
    max_job_attempts=3,
    shutdown_grace_seconds=30,
    retry_initial_backoff_seconds=0.5,
    retry_max_backoff_seconds=10,
    reconciliation_interval_seconds=60,
    orphan_grace_seconds=600,
    maintenance_batch_size=100,
)


def quiet_job_telemetry() -> Telemetry:
    """Telemetry for tests that do not observe spans or metrics."""
    return Telemetry(tracer=NoOpTracer(), measurements=Measurements(meter=NoOpMeter("test")))


def index_claim(*, cycle_attempts: int = 0, kind: JobKind = JobKind.INDEX) -> Claim:
    return Claim(
        created_at=NOW,
        job_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        kind=kind,
        generation=1,
        lease_owner="test-worker",
        attempts=cycle_attempts,
        cycle_attempts=cycle_attempts,
        trace_context={},
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Failure:
    category: ErrorCategory
    delay_seconds: float | None


@dataclass(slots=True, kw_only=True)
class RecordingQueue:
    """Mutated by the job under test; tests read what it settled."""

    failures: list[Failure] = field(default_factory=list)
    releases: list[bool] = field(default_factory=list)
    started: int = 0

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: float) -> tuple[Claim, ...]:
        return ()

    async def start_attempt(self, *, claim: Claim) -> Claim:
        self.started += 1
        return claim.model_copy(
            update={"attempts": claim.attempts + 1, "cycle_attempts": claim.cycle_attempts + 1}
        )

    async def release(self, *, claim: Claim, attempt_started: bool) -> None:
        self.releases.append(attempt_started)

    async def heartbeat(self, *, claim: Claim, lease_seconds: float) -> None:
        return None

    async def fail(
        self, *, claim: Claim, category: ErrorCategory, delay_seconds: float | None
    ) -> None:
        self.failures.append(Failure(category=category, delay_seconds=delay_seconds))

    async def acquire_permit(self, *, token: UUID, cap: int, lease_seconds: float) -> int | None:
        return 1

    async def release_permit(self, *, slot: int, token: UUID) -> None:
        return None


@dataclass(slots=True, kw_only=True)
class RecordingIndex:
    """One complete manifest of `texts`; mutated as chunks settle."""

    texts: tuple[str, ...] = ("evidence",)
    completed: list[UUID] = field(default_factory=list)
    published: bool = False
    chunk_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        self.chunk_ids = tuple(uuid4() for _ in self.texts)

    async def version(self, *, claim: Claim) -> VersionWork:
        return VersionWork(
            version_id=claim.version_id or uuid4(),
            filename="a.pdf",
            file_type=FileType.PDF,
            pipeline=Pipeline(
                window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
            ),
            object_key="attempts/a.pdf",
            object_version_id="v1",
            manifest_complete=True,
        )

    async def save_manifest(self, *, claim: Claim, chunks: tuple[Chunk, ...]) -> None:
        return None

    async def unfinished(self, *, claim: Claim) -> tuple[ChunkWork, ...]:
        return tuple(
            ChunkWork(
                next_retry_at=None, chunk_id=chunk_id, text=text, attempts=0, cycle_attempts=0
            )
            for chunk_id, text in zip(self.chunk_ids, self.texts, strict=True)
            if chunk_id not in self.completed
        )

    async def begin_chunk(self, *, claim: Claim, chunk_id: UUID, max_attempts: int) -> None:
        return None

    async def complete_chunk(
        self, *, claim: Claim, chunk_id: UUID, vector: tuple[float, ...]
    ) -> None:
        self.completed.append(chunk_id)

    async def retry_chunk(
        self, *, claim: Claim, chunk_id: UUID, category: ErrorCategory, delay_seconds: float
    ) -> None:
        return None

    async def publish(self, *, claim: Claim) -> None:
        self.published = True


@dataclass(frozen=True, slots=True, kw_only=True)
class NoCleanup:
    """Index attempts never reach cleanup."""

    async def cleanup_refs(self, *, claim: Claim) -> tuple[ObjectRef, ...]:
        raise NotImplementedError

    async def finish_cleanup(self, *, claim: Claim) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class UnusedStorage:
    """The manifest is already complete, so no original is read."""

    async def upload(self, *, path: Path, file_type: FileType) -> ObjectRef:
        raise NotImplementedError

    async def exists(self, *, ref: ObjectRef) -> bool:
        raise NotImplementedError

    async def remove(self, *, ref: ObjectRef) -> None:
        raise NotImplementedError

    async def download(self, *, ref: ObjectRef, path: Path) -> None:
        raise NotImplementedError

    async def orphan_candidates(self, *, limit: int) -> tuple[ObjectRef, ...]:
        return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class UnusedExtractor:
    async def extract(self, *, path: Path, file_type: FileType, filename: str) -> Extraction:
        raise NotImplementedError


async def no_wait(delay: float) -> None:
    return None


def job_context(
    *,
    embeddings: EmbeddingPort,
    queue: RecordingQueue,
    index: RecordingIndex,
    policy: WorkerPolicy | None = None,
) -> JobContext:
    return JobContext(
        index=index,
        cleanup=NoCleanup(),
        queue=queue,
        storage=UnusedStorage(),
        extractor=UnusedExtractor(),
        embeddings=embeddings,
        semaphore=asyncio.Semaphore(2),
        policy=policy or POLICY,
        sleep=no_wait,
        clock=lambda: NOW,
        id_factory=uuid4,
        jitter=lambda low, high: high,
        telemetry=quiet_job_telemetry(),
    )
