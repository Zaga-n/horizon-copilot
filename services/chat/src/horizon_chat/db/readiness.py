"""Read-only startup compatibility checks for app metadata and checkpoint migrations."""

from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, OperationalError, ProgrammingError
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_schema import EMBEDDING_DIMENSIONS, SCHEMA_REVISION


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabaseReadiness:
    """Checks schema compatibility without creating or modifying any object."""

    engine: AsyncEngine
    checkpoints: AsyncConnectionPool[AsyncConnection[DictRow]]
    embedding_model_id: str

    async def check(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                revision = await conn.scalar(text("SELECT version_num FROM app.alembic_version"))
                dimension = await conn.scalar(
                    text("""SELECT format_type(atttypid, atttypmod)
                    FROM pg_attribute WHERE attrelid = 'app.document_chunks'::regclass
                    AND attname = 'embedding'""")
                )
                incompatible = await conn.scalar(
                    text("""SELECT EXISTS (
                        SELECT 1 FROM app.documents d
                        JOIN app.document_versions v ON v.id = d.published_version_id
                        JOIN app.document_chunks c ON c.version_id = v.id
                        WHERE d.lifecycle = 'live' AND v.status = 'published'
                        AND c.status = 'completed' AND (
                            v.embedding_dimensions <> :dimensions
                            OR v.embedding_model_id <> :model OR c.embedding_model_id <> :model
                            OR c.embedding IS NULL OR btrim(c.filename) = ''
                            OR btrim(c.text) = '' OR c.file_type NOT IN ('pdf', 'docx')
                            OR jsonb_typeof(c.locators) <> 'array'
                        )
                    )"""),
                    {"dimensions": EMBEDDING_DIMENSIONS, "model": self.embedding_model_id},
                )
            async with self.checkpoints.connection() as conn:
                cursor = await conn.execute("SELECT max(v) AS version FROM checkpoint_migrations")
                row = await cursor.fetchone()
        except (DBAPIError, PoolTimeoutError, OperationalError, ProgrammingError, PoolTimeout):
            return False
        return (
            revision == SCHEMA_REVISION
            and dimension == f"vector({EMBEDDING_DIMENSIONS})"
            and not incompatible
            and row is not None
            and row["version"] == len(AsyncPostgresSaver.MIGRATIONS) - 1
        )
