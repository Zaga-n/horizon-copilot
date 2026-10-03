"""Technical job: delete stored originals that no version references once they are old enough."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_ingestion.db.tables import VERSIONS
from horizon_ingestion.db.transactions import object_lock, transaction
from horizon_ingestion.ports.uploads import ObjectStorage


@dataclass(frozen=True, slots=True, kw_only=True)
class OrphanReconciliation:
    """Each candidate is checked under the same object lock acceptance takes."""

    engine: AsyncEngine
    storage: ObjectStorage
    grace_seconds: float
    batch_size: int

    async def run_once(self) -> int:
        """Remove at most one listing page of orphans; returns how many were removed."""
        removed = 0
        for ref in await self.storage.orphan_candidates(limit=self.batch_size):
            async with transaction(self.engine) as conn:
                await object_lock(conn, key=ref.key)
                referenced = await conn.scalar(
                    select(VERSIONS.c.id)
                    .where(
                        VERSIONS.c.object_key == ref.key,
                        VERSIONS.c.object_version_id == ref.version_id,
                    )
                    .limit(1)
                )
                old = await conn.scalar(
                    select(
                        func.clock_timestamp()
                        > ref.created_at + timedelta(seconds=self.grace_seconds)
                    )
                )
                # The object fence intentionally spans this bounded deletion attempt.
                if referenced is None and old:
                    await self.storage.remove(ref=ref)
                    removed += 1
        return removed
