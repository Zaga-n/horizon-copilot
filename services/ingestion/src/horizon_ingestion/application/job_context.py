"""Collaborators, policy and injected effects that one job attempt uses; built by bootstrap."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from horizon_ingestion.domain.documents import WorkerPolicy
from horizon_ingestion.domain.retry_policy import Jitter
from horizon_ingestion.observability.tracing import Telemetry
from horizon_ingestion.ports.indexing import (
    CleanupStore,
    EmbeddingPort,
    ExtractionPort,
    IndexStore,
    WorkQueue,
)
from horizon_ingestion.ports.uploads import ObjectStorage

type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], datetime]
type IdFactory = Callable[[], UUID]


@dataclass(frozen=True, slots=True, kw_only=True)
class JobContext:
    """Collaborators and injected effects one job attempt uses."""

    index: IndexStore
    cleanup: CleanupStore
    queue: WorkQueue
    storage: ObjectStorage
    extractor: ExtractionPort
    embeddings: EmbeddingPort
    semaphore: asyncio.Semaphore
    policy: WorkerPolicy
    sleep: Sleep
    clock: Clock
    id_factory: IdFactory
    jitter: Jitter
    telemetry: Telemetry
