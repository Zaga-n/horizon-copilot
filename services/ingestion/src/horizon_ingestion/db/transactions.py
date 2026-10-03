"""One transaction owner, database clock and sanitized driver-failure translation."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_ingestion.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)


def translate_database_error(exc: Exception) -> Exception | None:
    """The port error for a driver failure; None means a programming error to re-raise."""
    if isinstance(exc, IntegrityError):
        return DataIntegrityError("database_integrity")
    if isinstance(exc, DataError):
        return RejectedError("database_rejected")
    # A socket failure outside the driver (connect, TLS) is an outage too, as in chat's copy.
    if isinstance(exc, OperationalError | InterfaceError | PoolTimeoutError | OSError) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    ):
        return DependencyUnavailableError("database_unavailable")
    return None


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            yield conn
    except (DBAPIError, PoolTimeoutError, OSError) as exc:
        translated = translate_database_error(exc)
        if translated is None:
            raise
        raise translated from exc


async def notify(conn: AsyncConnection) -> None:
    await conn.execute(text("NOTIFY ingestion_jobs"))


async def object_lock(conn: AsyncConnection, *, key: str) -> None:
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )
