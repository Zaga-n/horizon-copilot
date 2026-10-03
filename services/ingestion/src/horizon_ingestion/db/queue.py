"""Durable bounded claims, database-clock leases, global leased vendor slots, and the
LISTEN wake-up; notifications are hints, never the durable queue."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import psycopg
from pydantic import ValidationError
from sqlalchemy import RowMapping, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_ingestion.db.fencing import fence
from horizon_ingestion.db.tables import CHUNKS, JOBS, PERMITS, VERSIONS
from horizon_ingestion.db.transactions import transaction
from horizon_ingestion.domain.documents import ChunkState, ErrorCategory, JobKind, JobState
from horizon_ingestion.domain.lifecycle import ACTIVE_STATES, failure_settlement
from horizon_ingestion.ports.indexing import Claim

log = logging.getLogger(__name__)


def _decode_claims(rows: Sequence[RowMapping]) -> tuple[tuple[Claim, ...], tuple[RowMapping, ...]]:
    """Split claimed rows into decodable claims and the rows that no longer decode."""
    claims: list[Claim] = []
    corrupt: list[RowMapping] = []
    for row in rows:
        try:
            claims.append(Claim.model_validate(row))
        except ValidationError:
            corrupt.append(row)
    return tuple(claims), tuple(corrupt)


async def _settle_failure(
    conn: AsyncConnection,
    *,
    job_id: UUID,
    version_id: UUID | None,
    kind: JobKind,
    category: ErrorCategory,
    delay_seconds: float | None,
) -> None:
    """Apply the domain failure settlement to the job and, when terminal, its version."""
    settlement = failure_settlement(kind=kind, retrying=delay_seconds is not None)
    await conn.execute(
        update(JOBS)
        .where(JOBS.c.id == job_id)
        .values(
            status=settlement.state,
            error_category=category,
            lease_owner=None,
            lease_until=None,
            next_retry_at=func.now() + timedelta(seconds=delay_seconds or 0),
            updated_at=func.now(),
        )
    )
    if settlement.version is not None:
        await conn.execute(
            update(VERSIONS).where(VERSIONS.c.id == version_id).values(status=settlement.version)
        )
    if settlement.chunks is not None:
        await conn.execute(
            update(CHUNKS)
            .where(CHUNKS.c.version_id == version_id, CHUNKS.c.status != ChunkState.COMPLETED)
            .values(status=settlement.chunks, error_category=category, updated_at=func.now())
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PgWorkQueue:
    """Row locks end before processing; every result transition separately verifies the generation."""

    engine: AsyncEngine

    async def claim(self, *, worker_id: str, limit: int, lease_seconds: float) -> tuple[Claim, ...]:
        async with transaction(self.engine) as conn:
            due = (
                select(JOBS.c.id)
                .where(
                    JOBS.c.status.in_(ACTIVE_STATES),
                    JOBS.c.next_retry_at <= func.now(),
                    or_(JOBS.c.lease_until.is_(None), JOBS.c.lease_until <= func.now()),
                )
                .order_by(JOBS.c.next_retry_at, JOBS.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = (
                (
                    await conn.execute(
                        update(JOBS)
                        .where(JOBS.c.id.in_(due))
                        .values(
                            status=JobState.PROCESSING,
                            generation=JOBS.c.generation + 1,
                            lease_owner=worker_id,
                            lease_until=func.now() + timedelta(seconds=lease_seconds),
                            updated_at=func.now(),
                        )
                        .returning(
                            JOBS.c.created_at,
                            JOBS.c.id.label("job_id"),
                            JOBS.c.document_id,
                            JOBS.c.version_id,
                            JOBS.c.kind,
                            JOBS.c.generation,
                            JOBS.c.lease_owner,
                            JOBS.c.attempts,
                            JOBS.c.cycle_attempts,
                            JOBS.c.trace_context,
                        )
                    )
                )
                .mappings()
                .all()
            )
            claims, corrupt = _decode_claims(rows)
            for row in corrupt:
                # Settled like any terminal failure, so it cannot block later jobs and its version
                # and chunks do not stay candidate; its stored fields are kept for diagnosis.
                await _settle_failure(
                    conn,
                    job_id=row["job_id"],
                    version_id=row["version_id"],
                    kind=JobKind(row["kind"]),
                    category=ErrorCategory.INTEGRITY,
                    delay_seconds=None,
                )
                log.error("ingestion_job_corrupt", extra={"job_id": str(row["job_id"])})
            return claims

    async def start_attempt(self, *, claim: Claim) -> Claim:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            row = (
                (
                    await conn.execute(
                        update(JOBS)
                        .where(JOBS.c.id == claim.job_id)
                        .values(
                            attempts=JOBS.c.attempts + 1,
                            cycle_attempts=JOBS.c.cycle_attempts + 1,
                            updated_at=func.now(),
                        )
                        .returning(JOBS.c.attempts, JOBS.c.cycle_attempts)
                    )
                )
                .mappings()
                .one()
            )
            return claim.model_copy(
                update={"attempts": row["attempts"], "cycle_attempts": row["cycle_attempts"]}
            )

    async def release(self, *, claim: Claim, attempt_started: bool) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            uncount = 1 if attempt_started else 0
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(
                    attempts=JOBS.c.attempts - uncount,
                    cycle_attempts=JOBS.c.cycle_attempts - uncount,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=func.now(),
                )
            )
            if attempt_started and claim.version_id is not None:
                # Only the interrupted physical call is in `processing`; it did not settle.
                await conn.execute(
                    update(CHUNKS)
                    .where(
                        CHUNKS.c.version_id == claim.version_id,
                        CHUNKS.c.status == ChunkState.PROCESSING,
                    )
                    .values(
                        status=ChunkState.PENDING,
                        attempts=func.greatest(CHUNKS.c.attempts - 1, 0),
                        cycle_attempts=func.greatest(CHUNKS.c.cycle_attempts - 1, 0),
                        updated_at=func.now(),
                    )
                )

    async def heartbeat(self, *, claim: Claim, lease_seconds: float) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            await conn.execute(
                update(JOBS)
                .where(JOBS.c.id == claim.job_id)
                .values(
                    lease_until=func.now() + timedelta(seconds=lease_seconds), updated_at=func.now()
                )
            )

    async def fail(
        self, *, claim: Claim, category: ErrorCategory, delay_seconds: float | None
    ) -> None:
        async with transaction(self.engine) as conn:
            await fence(conn, claim=claim)
            await _settle_failure(
                conn,
                job_id=claim.job_id,
                version_id=claim.version_id,
                kind=claim.kind,
                category=category,
                delay_seconds=delay_seconds,
            )

    async def acquire_permit(self, *, token: UUID, cap: int, lease_seconds: float) -> int | None:
        async with transaction(self.engine) as conn:
            await conn.execute(
                pg_insert(PERMITS)
                .values([{"slot": slot} for slot in range(1, cap + 1)])
                .on_conflict_do_nothing()
            )
            candidate = select(PERMITS.c.slot).where(
                PERMITS.c.slot <= cap,
                or_(PERMITS.c.lease_until.is_(None), PERMITS.c.lease_until <= func.now()),
            )
            slot = await conn.scalar(
                update(PERMITS)
                .where(
                    PERMITS.c.slot.in_(
                        candidate.order_by(PERMITS.c.slot)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .values(token=token, lease_until=func.now() + timedelta(seconds=lease_seconds))
                .returning(PERMITS.c.slot)
            )
            return slot if isinstance(slot, int) else None

    async def release_permit(self, *, slot: int, token: UUID) -> None:
        async with transaction(self.engine) as conn:
            await conn.execute(
                update(PERMITS)
                .where(PERMITS.c.slot == slot, PERMITS.c.token == token)
                .values(token=None, lease_until=None)
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class JobListener:
    """Subscribe before requesting the startup/reconnect scan."""

    dsn: str
    connect_timeout_seconds: int
    scan_interval_seconds: float
    reconnect_max_seconds: float

    async def listen(
        self, *, wakeup: asyncio.Event, stop: asyncio.Event, connected: asyncio.Event | None = None
    ) -> None:
        delay = self.scan_interval_seconds
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.dsn, autocommit=True, connect_timeout=self.connect_timeout_seconds
                ) as conn:
                    await conn.execute("LISTEN ingestion_jobs")
                    if connected is not None:
                        connected.set()
                    wakeup.set()
                    delay = self.scan_interval_seconds
                    while not stop.is_set():
                        async for _notification in conn.notifies(
                            timeout=self.scan_interval_seconds
                        ):
                            wakeup.set()
            except (psycopg.Error, OSError):
                if connected is not None:
                    connected.clear()
                log.warning("ingestion_listener_reconnecting")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                delay = min(delay * 2, self.reconnect_max_seconds)
            finally:
                if connected is not None:
                    connected.clear()
