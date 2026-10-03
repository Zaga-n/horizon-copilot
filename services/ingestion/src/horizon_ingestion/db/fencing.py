"""The claim fence every result write takes: live lifecycle, generation, owner and lease."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from horizon_ingestion.db.tables import DOCUMENTS, JOBS
from horizon_ingestion.domain.documents import JobState
from horizon_ingestion.domain.lifecycle import claim_lifecycle
from horizon_ingestion.ports.indexing import Claim, StaleClaimError


async def fence(conn: AsyncConnection, *, claim: Claim) -> None:
    lifecycle = await conn.scalar(
        select(DOCUMENTS.c.lifecycle).where(DOCUMENTS.c.id == claim.document_id).with_for_update()
    )
    if lifecycle != claim_lifecycle(claim.kind):
        raise StaleClaimError("document_lifecycle_changed")
    valid = await conn.scalar(
        select(JOBS.c.id)
        .where(
            JOBS.c.id == claim.job_id,
            JOBS.c.generation == claim.generation,
            JOBS.c.lease_owner == claim.lease_owner,
            JOBS.c.lease_until > func.clock_timestamp(),
            JOBS.c.status == JobState.PROCESSING,
        )
        .with_for_update()
    )
    if valid is None:
        raise StaleClaimError("claim_expired")
