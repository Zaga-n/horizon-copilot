"""Owner-filtered job status, explicit retry and deletion fencing.

Only committed chunk states are counted, and deletion hides data before any cleanup I/O.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_ingestion.db.tables import CHUNKS, DOCUMENTS, JOBS, USERS, VERSIONS
from horizon_ingestion.db.transactions import notify, transaction
from horizon_ingestion.domain.documents import (
    ChunkState,
    ErrorCategory,
    JobKind,
    JobState,
    JobStatus,
    Lifecycle,
)
from horizon_ingestion.domain.lifecycle import (
    ACTIVE_STATES,
    DELETION,
    RESTART,
    RetryDecision,
    delete_available,
    new_job,
    retry_available,
    retry_decision,
)
from horizon_ingestion.ports.indexing import (
    IndexStoreIntegrityError,
    StatusConflictError,
    StatusNotFoundError,
)

log = logging.getLogger(__name__)


async def _status_row(conn: AsyncConnection, *, job_id: UUID) -> tuple[RowMapping, RowMapping]:
    """The job row with its document lifecycle, and the committed chunk counts."""
    row = (
        (
            await conn.execute(
                select(
                    JOBS,
                    DOCUMENTS.c.lifecycle,
                    VERSIONS.c.retired_at,
                    case(
                        (
                            JOBS.c.kind == JobKind.INDEX,
                            func.coalesce(VERSIONS.c.filename, DOCUMENTS.c.filename),
                        ),
                        else_=DOCUMENTS.c.filename,
                    ).label("filename"),
                )
                .join(DOCUMENTS, DOCUMENTS.c.id == JOBS.c.document_id)
                .outerjoin(VERSIONS, VERSIONS.c.id == JOBS.c.version_id)
                .where(JOBS.c.id == job_id)
            )
        )
        .mappings()
        .one()
    )
    counts = (
        (
            await conn.execute(
                select(
                    func.count().filter(CHUNKS.c.status == ChunkState.COMPLETED).label("completed"),
                    func.count().filter(CHUNKS.c.status == ChunkState.RETRYING).label("retrying"),
                    func.count().filter(CHUNKS.c.status == ChunkState.FAILED).label("failed"),
                ).where(CHUNKS.c.version_id == row["version_id"])
            )
        )
        .mappings()
        .one()
    )
    return row, counts


def _decode(row: RowMapping, counts: RowMapping) -> JobStatus:
    job_id = row["id"]
    try:
        retry = retry_available(
            status=JobState(row["status"]),
            kind=JobKind(row["kind"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            retired=row["retired_at"] is not None,
        )
        return JobStatus(
            document_id=row["document_id"],
            filename=row["filename"],
            version_id=row["version_id"],
            job_id=job_id,
            status=row["status"],
            stage=row["stage"],
            lifecycle=row["lifecycle"],
            total_chunks=row["total_chunks"],
            completed_chunks=counts["completed"],
            retrying_chunks=counts["retrying"],
            failed_chunks=counts["failed"],
            attempts=row["attempts"],
            retry_cycle=row["retry_cycle"],
            error_category=row["error_category"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            retry_available=retry,
        )
    except ValueError as exc:  # pydantic ValidationError and enum lookups alike
        raise IndexStoreIntegrityError("status_undecodable", row_id=job_id) from exc


def _integrity_status(row: RowMapping) -> JobStatus:
    """A listing entry for a row that no longer decodes, built from constrained columns only."""
    return JobStatus(
        document_id=row["document_id"],
        filename=None,
        version_id=row["version_id"],
        job_id=row["id"],
        status=JobState.FAILED,
        stage=row["stage"],
        lifecycle=row["lifecycle"],
        total_chunks=None,
        completed_chunks=0,
        retrying_chunks=0,
        failed_chunks=0,
        attempts=row["attempts"],
        retry_cycle=row["retry_cycle"],
        error_category=ErrorCategory.INTEGRITY,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        retry_available=False,
    )


async def _status(conn: AsyncConnection, *, job_id: UUID) -> JobStatus:
    row, counts = await _status_row(conn, job_id=job_id)
    return _decode(row, counts)


async def _owned_document(conn: AsyncConnection, *, subject: str, document_id: UUID) -> Lifecycle:
    """Lock the subject's document and return its lifecycle; others' documents are not found."""
    lifecycle = await conn.scalar(
        select(DOCUMENTS.c.lifecycle)
        .join(USERS)
        .where(DOCUMENTS.c.id == document_id, USERS.c.subject == subject)
        .with_for_update(of=DOCUMENTS)
    )
    if lifecycle is None:
        raise StatusNotFoundError("document_not_found")
    return Lifecycle(lifecycle)


async def _requeue(conn: AsyncConnection, *, job_id: UUID) -> None:
    """Start another fenced retry cycle of a failed job; earlier claims become stale."""
    await conn.execute(
        update(JOBS)
        .where(JOBS.c.id == job_id)
        .values(
            status=RESTART.job,
            cycle_attempts=0,
            retry_cycle=JOBS.c.retry_cycle + 1,
            generation=JOBS.c.generation + 1,
            error_category=None,
            lease_owner=None,
            lease_until=None,
            next_retry_at=func.now(),
            updated_at=func.now(),
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PgStatusStore:
    """One short transaction per poll, retry or deletion."""

    engine: AsyncEngine
    id_factory: Callable[[], UUID] = uuid4

    async def job_status(self, *, subject: str, job_id: UUID) -> JobStatus:
        async with transaction(self.engine) as conn:
            row = await conn.scalar(
                select(JOBS.c.id)
                .select_from(
                    JOBS.join(DOCUMENTS, JOBS.c.document_id == DOCUMENTS.c.id).join(
                        USERS, DOCUMENTS.c.user_id == USERS.c.id
                    )
                )
                .where(JOBS.c.id == job_id, USERS.c.subject == subject)
            )
            if row is None:
                raise StatusNotFoundError("job_not_found")
            return await _status(conn, job_id=job_id)

    async def document_status(self, *, subject: str, document_id: UUID) -> JobStatus:
        async with transaction(self.engine) as conn:
            await _owned_document(conn, subject=subject, document_id=document_id)
            job_id = await conn.scalar(
                select(JOBS.c.id)
                .where(
                    JOBS.c.document_id == document_id,
                    JOBS.c.kind.in_((JobKind.INDEX, JobKind.DELETE)),
                )
                .order_by(JOBS.c.created_at.desc(), JOBS.c.id.desc())
                .limit(1)
            )
            if not isinstance(job_id, UUID):
                raise StatusNotFoundError("job_not_found")
            return await _status(conn, job_id=job_id)

    async def list_documents(
        self, *, subject: str, limit: int, offset: int
    ) -> tuple[JobStatus, ...]:
        async with transaction(self.engine) as conn:
            latest = (
                select(JOBS.c.id)
                .where(
                    JOBS.c.document_id == DOCUMENTS.c.id,
                    JOBS.c.kind.in_((JobKind.INDEX, JobKind.DELETE)),
                )
                .order_by(JOBS.c.created_at.desc(), JOBS.c.id.desc())
                .limit(1)
                .correlate(DOCUMENTS)
                .scalar_subquery()
            )
            ids = (
                (
                    await conn.execute(
                        select(latest)
                        .select_from(DOCUMENTS.join(USERS))
                        .where(USERS.c.subject == subject)
                        .order_by(DOCUMENTS.c.created_at.desc(), DOCUMENTS.c.id)
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            listed: list[JobStatus] = []
            for job_id in ids:
                if not isinstance(job_id, UUID):
                    continue
                row, counts = await _status_row(conn, job_id=job_id)
                try:
                    listed.append(_decode(row, counts))
                except IndexStoreIntegrityError:
                    # One corrupt document must not hide the others; it is listed as an
                    # integrity fault.
                    log.exception("ingestion_status_corrupt", extra={"job_id": str(job_id)})
                    listed.append(_integrity_status(row))
            return tuple(listed)

    async def retry(self, *, subject: str, job_id: UUID) -> JobStatus:
        async with transaction(self.engine) as conn:
            document_id = await conn.scalar(select(JOBS.c.document_id).where(JOBS.c.id == job_id))
            if not isinstance(document_id, UUID):
                raise StatusNotFoundError("job_not_found")
            lifecycle = await _owned_document(conn, subject=subject, document_id=document_id)
            job = (
                (await conn.execute(select(JOBS).where(JOBS.c.id == job_id).with_for_update()))
                .mappings()
                .one()
            )
            retired = await conn.scalar(
                select(VERSIONS.c.retired_at).where(VERSIONS.c.id == job["version_id"])
            )
            decision = retry_decision(
                status=JobState(job["status"]),
                kind=JobKind(job["kind"]),
                lifecycle=lifecycle,
                retired=retired is not None,
            )
            if decision == RetryDecision.UNAVAILABLE:
                raise StatusConflictError("retry_unavailable")
            if decision == RetryDecision.NOT_FAILED:
                return await _status(conn, job_id=job_id)
            other = await conn.scalar(
                select(JOBS.c.id).where(
                    JOBS.c.document_id == document_id,
                    JOBS.c.id != job_id,
                    JOBS.c.kind == JobKind.INDEX,
                    JOBS.c.status.in_(ACTIVE_STATES),
                )
            )
            if other is not None:
                raise StatusConflictError("replacement_in_progress")
            await _requeue(conn, job_id=job_id)
            await conn.execute(
                update(CHUNKS)
                .where(
                    CHUNKS.c.version_id == job["version_id"],
                    CHUNKS.c.status != ChunkState.COMPLETED,
                )
                .values(
                    status=RESTART.chunks,
                    cycle_attempts=0,
                    error_category=None,
                    next_retry_at=None,
                    updated_at=func.now(),
                )
            )
            await conn.execute(
                update(VERSIONS)
                .where(VERSIONS.c.id == job["version_id"])
                .values(status=RESTART.version)
            )
            await notify(conn)
            return await _status(conn, job_id=job_id)

    async def delete(self, *, subject: str, document_id: UUID) -> None:
        async with transaction(self.engine) as conn:
            lifecycle = await _owned_document(conn, subject=subject, document_id=document_id)
            if not delete_available(lifecycle):
                if lifecycle == Lifecycle.DELETING:
                    failed = await conn.scalar(
                        select(JOBS.c.id)
                        .where(
                            JOBS.c.document_id == document_id,
                            JOBS.c.kind == JobKind.DELETE,
                            JOBS.c.status == JobState.FAILED,
                        )
                        .with_for_update()
                    )
                    if failed is not None:
                        await _requeue(conn, job_id=failed)
                        await notify(conn)
                return
            await conn.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.id == document_id)
                .values(
                    lifecycle=DELETION.document, published_version_id=None, updated_at=func.now()
                )
            )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.document_id == document_id, JOBS.c.kind != JobKind.DELETE)
                .values(
                    status=DELETION.other_jobs,
                    generation=JOBS.c.generation + 1,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=func.now(),
                )
            )
            await conn.execute(
                update(VERSIONS)
                .where(VERSIONS.c.document_id == document_id)
                .values(retired_at=func.now())
            )
            cleanup = new_job(JobKind.DELETE)
            await conn.execute(
                insert(JOBS).values(
                    id=self.id_factory(),
                    document_id=document_id,
                    kind=JobKind.DELETE,
                    status=cleanup.status,
                    stage=cleanup.stage,
                )
            )
            await notify(conn)
