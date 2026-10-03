"""Single SQLAlchemy transaction boundary translating known PostgreSQL failures."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.errors import InsufficientPrivilege
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_chat.ports.conversations import (
    ConversationStoreIntegrityError,
    ConversationStoreRejectedError,
    ConversationStoreUnavailableError,
)


def translate_database_error(exc: Exception) -> Exception | None:
    """The port error for a driver failure; None means a programming error to re-raise."""
    if isinstance(exc, IntegrityError):
        return ConversationStoreIntegrityError("database_constraint")
    if isinstance(exc, DataError):
        return ConversationStoreRejectedError("database_rejected")
    # A missing grant fails every request alike and waits for an operator, like an outage;
    # db/checkpoints.py classifies it the same way.
    if isinstance(exc, OperationalError | InterfaceError | PoolTimeoutError | OSError) or (
        isinstance(exc, DBAPIError)
        and (exc.connection_invalidated or isinstance(exc.orig, InsufficientPrivilege))
    ):
        return ConversationStoreUnavailableError("database_unavailable")
    return None


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    try:
        async with engine.begin() as conn:
            yield conn
    except (DBAPIError, PoolTimeoutError, OSError) as exc:
        translated = translate_database_error(exc)
        if translated is None:
            raise
        raise translated from exc
