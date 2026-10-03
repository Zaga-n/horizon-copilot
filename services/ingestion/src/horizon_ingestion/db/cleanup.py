"""Cleanup of deleted documents and superseded versions: exact refs, then content-free tombstones."""

from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_ingestion.db.fencing import fence
from horizon_ingestion.db.tables import CHUNKS, DOCUMENTS, JOBS, VERSIONS
from horizon_ingestion.db.transactions import transaction
from horizon_ingestion.domain.documents import JobKind
from horizon_ingestion.domain.lifecycle import cleanup_scope, cleanup_states
from horizon_ingestion.ports.indexing import Claim
from horizon_ingestion.ports.uploads import ObjectRef


@dataclass(frozen=True, slots=True, kw_only=True)
class PgCleanupStore:
    """One short fenced transaction per step; object removal happens between the two."""

    engine: AsyncEngine

    async def cleanup_refs(self, *, claim: Claim) -> tuple[ObjectRef, ...]:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            query = select(
                VERSIONS.c.object_key, VERSIONS.c.object_version_id, VERSIONS.c.created_at
            ).where(VERSIONS.c.document_id == claim.document_id, VERSIONS.c.object_key.is_not(None))
            if cleanup_scope(claim.kind).single_retired_version:
                query = query.where(
                    VERSIONS.c.id == claim.version_id, VERSIONS.c.retired_at.is_not(None)
                )
            rows = (await conn.execute(query)).mappings().all()
            return tuple(
                ObjectRef(
                    key=row["object_key"],
                    version_id=row["object_version_id"],
                    created_at=row["created_at"],
                )
                for row in rows
            )

    async def finish_cleanup(self, *, claim: Claim) -> None:
        states = cleanup_states(claim.kind)
        async with transaction(self.engine) as conn:
            scope = cleanup_scope(claim.kind)
            await fence(conn, claim=claim)
            versions = select(VERSIONS.c.id).where(VERSIONS.c.document_id == claim.document_id)
            if scope.single_retired_version:
                versions = versions.where(
                    VERSIONS.c.id == claim.version_id, VERSIONS.c.retired_at.is_not(None)
                )
            await conn.execute(delete(CHUNKS).where(CHUNKS.c.version_id.in_(versions)))
            await conn.execute(
                update(VERSIONS)
                .where(VERSIONS.c.id.in_(versions))
                .values(
                    corpus="",
                    object_key=None,
                    object_version_id=None,
                    filename=None,
                    title=None,
                    file_type=None,
                    project_metadata={},
                    pipeline_configuration={},
                    manifest_complete=False,
                )
            )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.version_id.in_(versions), JOBS.c.kind == JobKind.INDEX)
                .values(
                    status=states.index_jobs,
                    trace_context={},
                    total_chunks=None,
                    attempts=0,
                    cycle_attempts=0,
                    error_category=None,
                    updated_at=func.now(),
                )
            )
            if states.document is not None:
                await conn.execute(
                    update(DOCUMENTS)
                    .where(DOCUMENTS.c.id == claim.document_id)
                    .values(
                        lifecycle=states.document,
                        filename=None,
                        title=None,
                        file_type=None,
                        project_metadata={},
                        updated_at=func.now(),
                    )
                )
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(
                    status=states.job,
                    stage=states.stage,
                    lease_owner=None,
                    lease_until=None,
                    error_category=None,
                    trace_context={},
                    updated_at=func.now(),
                )
            )
