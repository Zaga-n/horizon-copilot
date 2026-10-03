"""Owner-scoped upload replay and content uniqueness commit behind the object fence."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_ingestion.db.tables import DOCUMENTS, JOBS, REQUESTS, USERS, VERSIONS
from horizon_ingestion.db.transactions import notify, object_lock, transaction
from horizon_ingestion.domain.acceptance import (
    AcceptanceConflict,
    UploadMetadata,
    content_match_conflict,
    replacement_conflict,
    replay_conflict,
    request_fingerprint,
)
from horizon_ingestion.domain.chunking import pipeline_fingerprint
from horizon_ingestion.domain.documents import Acceptance, JobKind, JobState, Lifecycle, Pipeline
from horizon_ingestion.domain.lifecycle import (
    ACTIVE_STATES,
    NEW_VERSION_STATE,
    new_job,
    retry_available,
)
from horizon_ingestion.ports.errors import DependencyUnavailableError
from horizon_ingestion.ports.uploads import (
    ConflictError,
    NotFoundError,
    ObjectRef,
    ObjectStorage,
    PreparedUpload,
)


async def owner(conn: AsyncConnection, *, subject: str, new_id: UUID) -> UUID:
    """The owner row for a subject, created on first use, then locked for this transaction."""
    await conn.execute(
        pg_insert(USERS)
        .values(id=new_id, subject=subject)
        .on_conflict_do_nothing(index_elements=[USERS.c.subject])
    )
    result = await conn.scalar(select(USERS.c.id).where(USERS.c.subject == subject))
    if not isinstance(result, UUID):
        raise DependencyUnavailableError("identity_row_missing")
    # READ COMMITTED: serialize this owner's request/content decisions after acquiring the lock.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 1))"), {"scope": str(result)}
    )
    return result


def raise_conflict(conflict: AcceptanceConflict | None) -> None:
    if conflict is not None:
        raise ConflictError(conflict.value)


async def accepted_job(conn: AsyncConnection, *, job_id: UUID, deduplicated: bool) -> Acceptance:
    row = (
        (
            await conn.execute(
                select(
                    JOBS.c.document_id,
                    JOBS.c.version_id,
                    JOBS.c.status,
                    JOBS.c.kind,
                    DOCUMENTS.c.lifecycle,
                    VERSIONS.c.retired_at,
                )
                .join(DOCUMENTS, DOCUMENTS.c.id == JOBS.c.document_id)
                .join(VERSIONS, VERSIONS.c.id == JOBS.c.version_id)
                .where(JOBS.c.id == job_id)
            )
        )
        .mappings()
        .one()
    )
    state = JobState(row["status"])
    retry = retry_available(
        status=state,
        kind=JobKind(row["kind"]),
        lifecycle=Lifecycle(row["lifecycle"]),
        retired=row["retired_at"] is not None,
    )
    return Acceptance(
        document_id=row["document_id"],
        version_id=row["version_id"],
        job_id=job_id,
        status=state,
        deduplicated=deduplicated,
        retry_available=retry,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PgAcceptanceStore:
    """Storage existence and expiry are checked under the same key lock as reconciliation."""

    engine: AsyncEngine
    storage: ObjectStorage
    id_factory: Callable[[], UUID] = uuid4

    async def find_existing(
        self, *, subject: str, key: str, sha256: str, metadata: UploadMetadata, pipeline: Pipeline
    ) -> Acceptance | None:
        async with transaction(self.engine) as conn:
            user_id = await owner(conn, subject=subject, new_id=self.id_factory())
            return await self._existing(
                conn,
                user_id=user_id,
                key=key,
                sha256=sha256,
                upload_metadata=metadata,
                pipeline=pipeline,
            )

    async def _existing(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        key: str,
        sha256: str,
        upload_metadata: UploadMetadata,
        pipeline: Pipeline,
    ) -> Acceptance | None:
        fingerprint = request_fingerprint(sha256=sha256, metadata=upload_metadata)
        replay = (
            (
                await conn.execute(
                    select(REQUESTS).where(
                        REQUESTS.c.user_id == user_id, REQUESTS.c.request_key == key
                    )
                )
            )
            .mappings()
            .first()
        )
        if replay is not None:
            raise_conflict(
                replay_conflict(
                    stored_fingerprint=replay["request_fingerprint"], fingerprint=fingerprint
                )
            )
            return await accepted_job(conn, job_id=replay["job_id"], deduplicated=True)
        await self._replacement_target(
            conn, user_id=user_id, document_id=upload_metadata.document_id
        )
        match = (
            (
                await conn.execute(
                    select(VERSIONS.c.id, VERSIONS.c.document_id, JOBS.c.id.label("job_id"))
                    .join(DOCUMENTS, DOCUMENTS.c.id == VERSIONS.c.document_id)
                    .join(
                        JOBS, (JOBS.c.version_id == VERSIONS.c.id) & (JOBS.c.kind == JobKind.INDEX)
                    )
                    .where(
                        VERSIONS.c.user_id == user_id,
                        VERSIONS.c.corpus == upload_metadata.corpus,
                        VERSIONS.c.sha256 == sha256,
                        VERSIONS.c.pipeline_fingerprint == pipeline_fingerprint(pipeline),
                        VERSIONS.c.retired_at.is_(None),
                        DOCUMENTS.c.lifecycle == Lifecycle.LIVE,
                    )
                )
            )
            .mappings()
            .first()
        )
        if match is None:
            return None
        raise_conflict(
            content_match_conflict(
                requested_document_id=upload_metadata.document_id,
                matched_document_id=match["document_id"],
            )
        )
        await conn.execute(
            insert(REQUESTS).values(
                user_id=user_id,
                request_key=key,
                request_fingerprint=fingerprint,
                document_id=match["document_id"],
                version_id=match["id"],
                job_id=match["job_id"],
            )
        )
        return await accepted_job(conn, job_id=match["job_id"], deduplicated=True)

    async def _replacement_target(
        self, conn: AsyncConnection, *, user_id: UUID, document_id: UUID | None
    ) -> None:
        if document_id is None:
            return
        row = (
            await conn.execute(
                select(DOCUMENTS.c.lifecycle)
                .where(DOCUMENTS.c.id == document_id, DOCUMENTS.c.user_id == user_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            raise NotFoundError("document_not_found")
        raise_conflict(
            replacement_conflict(lifecycle=Lifecycle(row[0]), has_active_index_job=False)
        )

    async def accept(
        self,
        *,
        subject: str,
        key: str,
        upload: PreparedUpload,
        metadata: UploadMetadata,
        pipeline: Pipeline,
        ref: ObjectRef,
        max_object_age_seconds: float,
        trace_context: dict[str, str],
    ) -> Acceptance:
        async with transaction(self.engine) as conn:
            user_id = await owner(conn, subject=subject, new_id=self.id_factory())
            existing = await self._existing(
                conn,
                user_id=user_id,
                key=key,
                sha256=upload.sha256,
                upload_metadata=metadata,
                pipeline=pipeline,
            )
            if existing is not None:
                return existing
            await object_lock(conn, key=ref.key)
            expired = select(
                func.clock_timestamp() > ref.created_at + timedelta(seconds=max_object_age_seconds)
            )
            if (
                await conn.scalar(expired)
                or not await self.storage.exists(ref=ref)
                or await conn.scalar(expired)
            ):
                raise DependencyUnavailableError("upload_finalization_expired")
            document_id = metadata.document_id or self.id_factory()
            if metadata.document_id is None:
                await conn.execute(
                    insert(DOCUMENTS).values(
                        id=document_id,
                        user_id=user_id,
                        filename=upload.filename,
                        file_type=upload.file_type,
                        project_metadata=metadata.project_metadata,
                    )
                )
            active = await conn.scalar(
                select(JOBS.c.id).where(
                    JOBS.c.document_id == document_id,
                    JOBS.c.kind == JobKind.INDEX,
                    JOBS.c.status.in_(ACTIVE_STATES),
                )
            )
            raise_conflict(
                replacement_conflict(
                    lifecycle=Lifecycle.LIVE, has_active_index_job=active is not None
                )
            )
            version_id, job_id = self.id_factory(), self.id_factory()
            start = new_job(JobKind.INDEX)
            await conn.execute(
                insert(VERSIONS).values(
                    id=version_id,
                    document_id=document_id,
                    user_id=user_id,
                    status=NEW_VERSION_STATE,
                    sha256=upload.sha256,
                    pipeline_fingerprint=pipeline_fingerprint(pipeline),
                    corpus=metadata.corpus,
                    object_key=ref.key,
                    object_version_id=ref.version_id,
                    embedding_model_id=pipeline.embedding_model_id,
                    embedding_dimensions=pipeline.embedding_dimensions,
                    pipeline_configuration=pipeline.model_dump(mode="json"),
                    filename=upload.filename,
                    file_type=upload.file_type,
                    project_metadata=metadata.project_metadata,
                )
            )
            await conn.execute(
                insert(JOBS).values(
                    id=job_id,
                    document_id=document_id,
                    version_id=version_id,
                    kind=JobKind.INDEX,
                    status=start.status,
                    stage=start.stage,
                    trace_context=trace_context,
                )
            )
            await conn.execute(
                insert(REQUESTS).values(
                    user_id=user_id,
                    request_key=key,
                    request_fingerprint=request_fingerprint(
                        sha256=upload.sha256, metadata=metadata
                    ),
                    document_id=document_id,
                    version_id=version_id,
                    job_id=job_id,
                )
            )
            await notify(conn)
            return await accepted_job(conn, job_id=job_id, deduplicated=False)
