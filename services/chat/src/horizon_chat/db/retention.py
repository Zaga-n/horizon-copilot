"""Purge inactive conversations and their LangGraph threads, one replica at a time.

Coordination is a session advisory lock (work-queues.md, rung 2): every step is
idempotent, the time-based predicate catches up after downtime, and a crashed
pass leaves PURGING rows that the next pass finishes.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_chat.db.ledger import database_now, recover_expired_run
from horizon_chat.db.tables import CONVERSATIONS
from horizon_chat.db.transactions import transaction, translate_database_error
from horizon_chat.domain.conversations import ConversationStatus
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.checkpoints import CheckpointStore, CheckpointStoreUnavailableError
from horizon_chat.ports.errors import DataIntegrityError

logger = logging.getLogger(__name__)
JOB_LOCK = "conversation_retention"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionPass:
    """One batch: `more_due` asks the supervisor to run the next batch without waiting."""

    purged: int
    failed: int
    more_due: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRetention:
    engine: AsyncEngine
    checkpoints: CheckpointStore
    retention_days: int
    batch_size: int
    checkpoint_timeout_seconds: float
    telemetry: Telemetry

    async def run_once(self) -> RetentionPass:
        """Purge at most one batch, so shutdown never waits for a whole backlog.

        Nothing is purged when another replica holds the lock. More is due only after a full
        batch that made progress: failed conversations sort last, so a full batch of failures
        means the rest waits for the next interval instead of retrying them back to back.
        """
        async with self._job_lock() as locked:
            if not locked:
                return RetentionPass(purged=0, failed=0, more_due=False)
            with self.telemetry.work("maintenance"):
                # An outage propagates to the loop supervisor; the next pass resumes the fence.
                batch = await self._fence_batch()
                purged = 0
                for conversation_id in batch:
                    if await self._purge(conversation_id):
                        purged += 1
                failed = len(batch) - purged
                if batch:
                    logger.info(
                        "retention_completed",
                        extra={"purged_count": purged, "failed_count": failed},
                    )
                return RetentionPass(
                    purged=purged,
                    failed=failed,
                    more_due=len(batch) == self.batch_size and purged > 0,
                )

    async def _purge(self, conversation_id: UUID) -> bool:
        """False means this conversation's stored data is inconsistent; it is left degraded.

        An outage still raises: it stops the pass, and the next pass resumes the fence.
        """
        try:
            async with asyncio.timeout(self.checkpoint_timeout_seconds):
                await self.checkpoints.delete_thread(conversation_id=conversation_id)
        except TimeoutError as exc:
            raise CheckpointStoreUnavailableError("checkpoint_timeout") from exc
        except DataIntegrityError:
            await self._record_failure(conversation_id)
            return False
        try:
            await self._delete_fenced(conversation_id)
        except DataIntegrityError:
            await self._record_failure(conversation_id)
            return False
        self.telemetry.measurements.purged.add(1)
        return True

    async def _record_failure(self, conversation_id: UUID) -> None:
        logger.warning(
            "retention_item_failed",
            extra={"conversation_id": str(conversation_id)},
            exc_info=True,
        )
        await self._defer(conversation_id)
        self.telemetry.measurements.purge_failures.add(1)

    async def _defer(self, conversation_id: UUID) -> None:
        """Move a failing conversation behind every other due one in later passes."""
        async with transaction(self.engine) as conn:
            await conn.execute(
                update(CONVERSATIONS)
                .where(CONVERSATIONS.c.id == conversation_id)
                .values(updated_at=await database_now(conn))
            )

    @asynccontextmanager
    async def _job_lock(self) -> AsyncIterator[bool]:
        lock = func.hashtext(JOB_LOCK)
        try:
            async with self.engine.connect() as conn:
                locked = bool(await conn.scalar(select(func.pg_try_advisory_lock(lock))))
                # The session lock outlives this commit; no transaction stays open.
                await conn.commit()
                try:
                    yield locked
                finally:
                    if locked:
                        try:
                            await conn.scalar(select(func.pg_advisory_unlock(lock)))
                            await conn.commit()
                        except BaseException:
                            # A session lock survives on a pooled connection; drop it instead.
                            await conn.invalidate()
                            raise
        except (DBAPIError, PoolTimeoutError, OSError) as exc:
            translated = translate_database_error(exc)
            if translated is None:
                raise
            raise translated from exc

    async def _fence_batch(self) -> tuple[UUID, ...]:
        """Mark due conversations PURGING so no new turn can start on a half-deleted one."""
        async with transaction(self.engine) as conn:
            now = await database_now(conn)
            cutoff = now - timedelta(days=self.retention_days)
            rows = (
                (
                    await conn.execute(
                        select(CONVERSATIONS)
                        .where(
                            or_(
                                CONVERSATIONS.c.status == ConversationStatus.PURGING,
                                (CONVERSATIONS.c.last_activity_at < cutoff)
                                & or_(
                                    CONVERSATIONS.c.active_run_id.is_(None),
                                    CONVERSATIONS.c.lease_until <= now,
                                ),
                            ),
                        )
                        # Deferred failures carry a fresh updated_at and so sort last.
                        .order_by(CONVERSATIONS.c.updated_at, CONVERSATIONS.c.id)
                        .limit(self.batch_size)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                await recover_expired_run(conn, row, now)
                await conn.execute(
                    update(CONVERSATIONS)
                    .where(CONVERSATIONS.c.id == row["id"])
                    .values(status=ConversationStatus.PURGING, updated_at=now)
                )
            return tuple(row["id"] for row in rows)

    async def _delete_fenced(self, conversation_id: UUID) -> None:
        async with transaction(self.engine) as conn:
            await conn.execute(
                delete(CONVERSATIONS).where(
                    CONVERSATIONS.c.id == conversation_id,
                    CONVERSATIONS.c.status == ConversationStatus.PURGING,
                )
            )
