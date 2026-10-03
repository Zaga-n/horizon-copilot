"""The index store: the version being indexed, its manifest, per-chunk progress and publication.

Each method is one short fenced transaction; locks never span extraction or embedding calls,
and the document row is locked before the job and chunks.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import RowMapping, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_ingestion.db.fencing import fence
from horizon_ingestion.db.tables import CHUNKS, DOCUMENTS, JOBS, VERSIONS
from horizon_ingestion.db.transactions import notify, transaction
from horizon_ingestion.domain.chunking import Chunk
from horizon_ingestion.domain.documents import ChunkState, ErrorCategory, JobKind, Stage
from horizon_ingestion.domain.lifecycle import (
    NEW_CHUNK_STATE,
    PUBLICATION,
    chunk_stage,
    manifest_publishable,
    new_job,
    superseded_version,
)
from horizon_ingestion.ports.indexing import (
    ChunkBudgetExhaustedError,
    ChunkNotProcessingError,
    ChunkWork,
    Claim,
    EmptyManifestError,
    IndexStoreIntegrityError,
    ManifestIncompleteError,
    VersionWork,
)


def _chunk_work(row: RowMapping) -> ChunkWork:
    try:
        return ChunkWork.model_validate(row)
    except ValidationError as exc:
        raise IndexStoreIntegrityError("chunk_undecodable", row_id=row["chunk_id"]) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class PgIndexStore:
    """Vectors land only for the claim owner; publication is all-or-nothing."""

    engine: AsyncEngine
    id_factory: Callable[[], UUID] = uuid4

    async def version(self, *, claim: Claim) -> VersionWork:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            row = (
                (
                    await conn.execute(
                        select(
                            VERSIONS.c.id.label("version_id"),
                            VERSIONS.c.filename,
                            VERSIONS.c.file_type,
                            VERSIONS.c.pipeline_configuration.label("pipeline"),
                            VERSIONS.c.object_key,
                            VERSIONS.c.object_version_id,
                            VERSIONS.c.manifest_complete,
                        ).where(VERSIONS.c.id == claim.version_id)
                    )
                )
                .mappings()
                .one()
            )
            try:
                return VersionWork.model_validate(row)
            except ValidationError as exc:
                raise IndexStoreIntegrityError(
                    "version_undecodable", row_id=row["version_id"]
                ) from exc

    async def save_manifest(self, *, claim: Claim, chunks: tuple[Chunk, ...]) -> None:
        if not chunks:
            raise EmptyManifestError()
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            complete = await conn.scalar(
                select(VERSIONS.c.manifest_complete).where(VERSIONS.c.id == claim.version_id)
            )
            if complete:
                return
            model = await conn.scalar(
                select(VERSIONS.c.embedding_model_id).where(VERSIONS.c.id == claim.version_id)
            )
            await conn.execute(
                insert(CHUNKS),
                [
                    {
                        "id": chunk.id,
                        "version_id": claim.version_id,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "content_hash": chunk.content_hash,
                        "title": chunk.title,
                        "filename": chunk.filename,
                        "file_type": chunk.file_type,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        "locators": [locator.model_dump() for locator in chunk.locators],
                        "page": chunk.locators[0].page if chunk.locators else None,
                        "section_heading": chunk.locators[0].section_heading
                        if chunk.locators
                        else None,
                        "status": NEW_CHUNK_STATE,
                        "embedding_model_id": model,
                    }
                    for chunk in chunks
                ],
            )
            await conn.execute(
                update(VERSIONS)
                .where(VERSIONS.c.id == claim.version_id)
                .values(manifest_complete=True, title=chunks[0].title)
            )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(total_chunks=len(chunks), stage=Stage.EMBEDDING, updated_at=func.now())
            )

    async def unfinished(self, *, claim: Claim) -> tuple[ChunkWork, ...]:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            rows = (
                (
                    await conn.execute(
                        select(
                            CHUNKS.c.id.label("chunk_id"),
                            CHUNKS.c.text,
                            CHUNKS.c.next_retry_at,
                            CHUNKS.c.attempts,
                            CHUNKS.c.cycle_attempts,
                        )
                        .where(
                            CHUNKS.c.version_id == claim.version_id,
                            CHUNKS.c.status != ChunkState.COMPLETED,
                        )
                        .order_by(CHUNKS.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            return tuple(_chunk_work(row) for row in rows)

    async def begin_chunk(self, *, claim: Claim, chunk_id: UUID, max_attempts: int) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            result = await conn.scalar(
                update(CHUNKS)
                .where(
                    CHUNKS.c.id == chunk_id,
                    CHUNKS.c.version_id == claim.version_id,
                    CHUNKS.c.status != ChunkState.COMPLETED,
                    CHUNKS.c.cycle_attempts < max_attempts,
                )
                .values(
                    status=ChunkState.PROCESSING,
                    attempts=CHUNKS.c.attempts + 1,
                    cycle_attempts=CHUNKS.c.cycle_attempts + 1,
                    error_category=None,
                    next_retry_at=None,
                    updated_at=func.now(),
                )
                .returning(CHUNKS.c.id)
            )
            if result is None:
                raise ChunkBudgetExhaustedError()

    async def complete_chunk(
        self, *, claim: Claim, chunk_id: UUID, vector: tuple[float, ...]
    ) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            result = await conn.scalar(
                update(CHUNKS)
                .where(
                    CHUNKS.c.id == chunk_id,
                    CHUNKS.c.version_id == claim.version_id,
                    CHUNKS.c.status == ChunkState.PROCESSING,
                )
                .values(
                    embedding=list(vector),
                    status=ChunkState.COMPLETED,
                    error_category=None,
                    next_retry_at=None,
                    updated_at=func.now(),
                )
                .returning(CHUNKS.c.id)
            )
            if result is None:
                raise ChunkNotProcessingError()
            remaining = await conn.scalar(
                select(func.count())
                .select_from(CHUNKS)
                .where(
                    CHUNKS.c.version_id == claim.version_id, CHUNKS.c.status != ChunkState.COMPLETED
                )
            )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(stage=chunk_stage(unfinished_chunks=remaining or 0), updated_at=func.now())
            )

    async def retry_chunk(
        self, *, claim: Claim, chunk_id: UUID, category: ErrorCategory, delay_seconds: float
    ) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            await conn.execute(
                update(CHUNKS)
                .where(
                    CHUNKS.c.id == chunk_id,
                    CHUNKS.c.version_id == claim.version_id,
                    CHUNKS.c.status != ChunkState.COMPLETED,
                )
                .values(
                    status=ChunkState.RETRYING,
                    error_category=category,
                    next_retry_at=func.now() + timedelta(seconds=delay_seconds),
                    updated_at=func.now(),
                )
            )

    async def publish(self, *, claim: Claim) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            version = (
                (await conn.execute(select(VERSIONS).where(VERSIONS.c.id == claim.version_id)))
                .mappings()
                .one()
            )
            unfinished = await conn.scalar(
                select(func.count())
                .select_from(CHUNKS)
                .where(
                    CHUNKS.c.version_id == claim.version_id, CHUNKS.c.status != ChunkState.COMPLETED
                )
            )
            count = await conn.scalar(
                select(func.count())
                .select_from(CHUNKS)
                .where(CHUNKS.c.version_id == claim.version_id)
            )
            total = await conn.scalar(select(JOBS.c.total_chunks).where(JOBS.c.id == claim.job_id))
            if not manifest_publishable(
                manifest_complete=version["manifest_complete"],
                total_chunks=total,
                stored_chunks=count or 0,
                unfinished_chunks=unfinished or 0,
            ):
                raise ManifestIncompleteError()
            previous = superseded_version(
                published_version_id=await conn.scalar(
                    select(DOCUMENTS.c.published_version_id).where(
                        DOCUMENTS.c.id == claim.document_id
                    )
                ),
                candidate_id=version["id"],
            )
            if previous is not None:
                cleanup = new_job(JobKind.SUPERSEDED_CLEANUP)
                await conn.execute(
                    update(VERSIONS)
                    .where(VERSIONS.c.id == previous)
                    .values(status=PUBLICATION.replaced, retired_at=func.now())
                )
                await conn.execute(
                    insert(JOBS).values(
                        id=self.id_factory(),
                        document_id=claim.document_id,
                        version_id=previous,
                        kind=JobKind.SUPERSEDED_CLEANUP,
                        status=cleanup.status,
                        stage=cleanup.stage,
                    )
                )
            await conn.execute(
                update(VERSIONS)
                .where(VERSIONS.c.id == claim.version_id)
                .values(status=PUBLICATION.candidate, updated_at=func.now())
            )
            await conn.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.id == claim.document_id)
                .values(
                    published_version_id=claim.version_id,
                    filename=version["filename"],
                    title=version["title"],
                    file_type=version["file_type"],
                    project_metadata=version["project_metadata"],
                    updated_at=func.now(),
                )
            )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(
                    status=PUBLICATION.job,
                    stage=PUBLICATION.stage,
                    error_category=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=func.now(),
                )
            )
            await notify(conn)
