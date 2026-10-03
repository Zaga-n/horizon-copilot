"""Index one version: extract once, embed unfinished chunks under vendor budgets, publish."""

import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from horizon_ingestion.application.failures import ChunkDeadlineExceededError, failure_category
from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.domain.chunking import build_manifest
from horizon_ingestion.domain.retry_policy import backoff
from horizon_ingestion.ports.errors import DependencyUnavailableError, RejectedError
from horizon_ingestion.ports.indexing import (
    ChunkWork,
    Claim,
    EmbeddingProtocolError,
    EmbeddingUnavailableError,
)
from horizon_ingestion.ports.uploads import ObjectRef


@dataclass(slots=True)
class JobDeadline:
    """The job's wall-clock budget, extended by time spent waiting; mutated by `waiting`.

    Waiting for a vendor permit or out a recorded backoff is not work, so a busy vendor pool
    cannot exhaust a job's budget on its own.
    """

    timeout: asyncio.Timeout

    @asynccontextmanager
    async def waiting(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        when = self.timeout.when()
        self.timeout.reschedule(None)
        try:
            yield
        finally:
            if when is not None and not self.timeout.expired():
                self.timeout.reschedule(when + loop.time() - started)


async def index_version(*, claim: Claim, context: JobContext, deadline: JobDeadline) -> None:
    index, telemetry = context.index, context.telemetry
    version = await index.version(claim=claim)
    if not version.manifest_complete:
        with tempfile.TemporaryDirectory(prefix="horizon-extract-") as directory:
            path = Path(directory) / "original"
            await context.storage.download(
                ref=ObjectRef(
                    key=version.object_key,
                    version_id=version.object_version_id,
                    created_at=context.clock(),
                ),
                path=path,
            )
            with telemetry.work("extraction"):
                extraction = await context.extractor.extract(
                    path=path, file_type=version.file_type, filename=version.filename
                )
        manifest = build_manifest(
            version_id=version.version_id,
            extraction=extraction,
            filename=version.filename,
            file_type=version.file_type,
            pipeline=version.pipeline,
        )
        await index.save_manifest(claim=claim, chunks=manifest)
    for chunk in await index.unfinished(claim=claim):
        if chunk.next_retry_at is not None:
            # Waiting out a recorded chunk backoff is not provider time.
            async with deadline.waiting():
                await context.sleep(max(0, (chunk.next_retry_at - context.clock()).total_seconds()))
        await _embed_chunk(claim=claim, chunk=chunk, context=context, deadline=deadline)
    with telemetry.work("publication"):
        await index.publish(claim=claim)


@dataclass(slots=True)
class _ProviderBudget:
    """Mutated only by `spend`: provider time left for one chunk across its physical calls."""

    remaining_seconds: float

    @asynccontextmanager
    async def spend(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(self.remaining_seconds):
                yield
        except TimeoutError as exc:
            raise ChunkDeadlineExceededError() from exc
        finally:
            self.remaining_seconds -= loop.time() - started


async def _embed_chunk(
    *, claim: Claim, chunk: ChunkWork, context: JobContext, deadline: JobDeadline
) -> None:
    policy, index, telemetry = context.policy, context.index, context.telemetry
    budget = _ProviderBudget(remaining_seconds=policy.chunk_timeout_seconds)
    for attempt in range(1, policy.max_call_attempts + 1):
        await index.begin_chunk(
            claim=claim, chunk_id=chunk.chunk_id, max_attempts=policy.max_chunk_attempts
        )
        try:
            vector = await _physical_embedding(
                text=chunk.text, budget=budget, context=context, deadline=deadline
            )
        except (EmbeddingUnavailableError, EmbeddingProtocolError) as exc:
            category = failure_category(exc)
            delay = backoff(
                attempt=attempt,
                policy=policy,
                jitter=context.jitter,
                retry_after_seconds=exc.retry_after_seconds,
            )
            # Settle the failed call even on the last attempt: a chunk left `processing` would
            # be un-counted by a later claim release as if its call had been interrupted.
            await index.retry_chunk(
                claim=claim, chunk_id=chunk.chunk_id, category=category, delay_seconds=delay
            )
            if attempt == policy.max_call_attempts:
                raise
            telemetry.chunk_retried(category=category.value)
            async with deadline.waiting():
                await context.sleep(delay)
        else:
            await index.complete_chunk(claim=claim, chunk_id=chunk.chunk_id, vector=vector)
            telemetry.chunk_completed()
            return


async def _physical_embedding(
    *, text: str, budget: _ProviderBudget, context: JobContext, deadline: JobDeadline
) -> tuple[float, ...]:
    async with deadline.waiting():
        await context.semaphore.acquire()
    try:
        async with _vendor_permit(context=context, deadline=deadline):
            task = asyncio.create_task(context.embeddings.embed(text=text))
            async with budget.spend():
                # Cancellation cannot release a permit while LangChain's executor call is still running.
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    with suppress(DependencyUnavailableError, RejectedError):
                        await task
                    raise
    finally:
        context.semaphore.release()


@asynccontextmanager
async def _vendor_permit(*, context: JobContext, deadline: JobDeadline) -> AsyncIterator[None]:
    """Hold one global vendor slot; the wait for a free slot is not provider time."""
    policy, queue = context.policy, context.queue
    token = context.id_factory()
    slot = await queue.acquire_permit(
        token=token, cap=policy.vendor_concurrency, lease_seconds=policy.permit_lease_seconds
    )
    while slot is None:
        async with deadline.waiting():
            await context.sleep(policy.scan_interval_seconds)
        slot = await queue.acquire_permit(
            token=token, cap=policy.vendor_concurrency, lease_seconds=policy.permit_lease_seconds
        )
    try:
        yield
    finally:
        await queue.release_permit(slot=slot, token=token)
