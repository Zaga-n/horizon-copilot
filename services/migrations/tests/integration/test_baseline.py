"""Fresh replay, checkpoint isolation, and real PostgreSQL constraints."""

import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from horizon_schema import SCHEMA_REVISION, metadata

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parents[4] / "services/migrations/alembic.ini"


async def test_fresh_replay_and_foreign_schema_has_no_diff(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        cursor = await conn.execute("SELECT version_num FROM app.alembic_version")
        assert await cursor.fetchone() == (SCHEMA_REVISION,)
        cursor = await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app'")
        tables = {row[0] for row in await cursor.fetchall()}
        assert tables == {t.name for t in metadata.tables.values()} | {"alembic_version"}
        await conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
        await conn.execute("CREATE TABLE langgraph.unrelated_checkpoint (id text PRIMARY KEY)")
    config = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.check, config)
    await asyncio.to_thread(command.upgrade, config, "head")


async def test_vector_and_version_dimensions_are_enforced(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        cursor = await conn.execute("""SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a WHERE a.attrelid = 'app.document_chunks'::regclass
            AND a.attname = 'embedding'""")
        assert await cursor.fetchone() == ("vector(1024)",)
        with pytest.raises(psycopg.errors.DataException):
            await conn.execute(
                """INSERT INTO app.document_chunks
                (id, version_id, ordinal, text, embedding, filename, file_type, status, embedding_model_id)
                VALUES (%s, %s, 0, 'text', '[1,2]', 'test.pdf', 'pdf', 'completed', 'titan')""",
                (uuid4(), uuid4()),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                """INSERT INTO app.document_versions
                (id, document_id, status, sha256, pipeline_fingerprint, object_key, object_version_id,
                 embedding_model_id, embedding_dimensions)
                VALUES (%s, %s, 'candidate', %s, %s, 'key', 'v1', 'titan', 512)""",
                (uuid4(), uuid4(), "a" * 64, "b" * 64),
            )


async def test_blank_thread_feedback_is_rejected(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                """INSERT INTO app.thread_feedback
                (user_id, conversation_id, comment, context_message_order) VALUES (%s, %s, '   ', 0)""",
                (uuid4(), uuid4()),
            )
