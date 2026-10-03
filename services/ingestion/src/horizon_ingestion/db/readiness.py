"""Verify the application revision without granting schema ownership to ingestion."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_schema import EMBEDDING_DIMENSIONS, SCHEMA_REVISION


@dataclass(frozen=True, slots=True, kw_only=True)
class Readiness:
    """A missing or incompatible migration, or vectors the index cannot store, make it unready."""

    engine: AsyncEngine
    embedding_dimensions: int

    async def check(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                actual = await conn.scalar(text("SELECT version_num FROM app.alembic_version"))
        except (DBAPIError, PoolTimeoutError):
            return False
        return (
            isinstance(actual, str)
            and actual == SCHEMA_REVISION
            and self.embedding_dimensions == EMBEDDING_DIMENSIONS
        )
