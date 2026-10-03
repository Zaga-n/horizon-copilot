"""Real PostgreSQL enforces owner/configuration deduplication and storage boundaries."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[4]


async def test_owner_configuration_uniqueness(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        owner = uuid4()
        document = uuid4()
        await conn.execute(
            "INSERT INTO app.users (id, subject) VALUES (%s, 'test-owner')", (owner,)
        )
        await conn.execute(
            """INSERT INTO app.documents (id, user_id, filename, file_type)
                            VALUES (%s, %s, 'a.pdf', 'pdf')""",
            (document, owner),
        )
        query = """INSERT INTO app.document_versions (id, document_id, user_id, status, sha256,
            pipeline_fingerprint, object_key, object_version_id, embedding_model_id, embedding_dimensions)
            VALUES (%s, %s, %s, 'candidate', %s, %s, 'key', 'v1', 'titan', 1024)"""
        await conn.execute(query, (uuid4(), document, owner, "a" * 64, "b" * 64))
        with pytest.raises(psycopg.errors.UniqueViolation, match="live_content"):
            await conn.execute(query, (uuid4(), document, owner, "a" * 64, "b" * 64))
        await conn.execute(query, (uuid4(), document, owner, "a" * 64, "c" * 64))
        await conn.execute(
            "UPDATE app.document_versions SET retired_at = now() WHERE document_id = %s",
            (document,),
        )
        await conn.execute(query, (uuid4(), document, owner, "a" * 64, "b" * 64))


async def test_ingestion_role_cannot_read_chat_or_create_schema(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute((ROOT / "services/migrations/sql/runtime_grants.sql").read_text())
        await conn.execute("SET ROLE ingestion_runtime")
        await conn.execute("SELECT id, subject FROM app.users")
        await conn.execute("SELECT id FROM app.ingestion_jobs")
        await conn.execute("SELECT slot FROM app.vendor_permits")
        for statement in (
            "SELECT content FROM app.messages",
            "SELECT * FROM app.conversations",
            "SELECT * FROM langgraph.checkpoints",
            "CREATE TABLE app.illegal (id integer)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(statement)


async def test_job_generation_and_lease_pair_are_constrained(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="counters"):
            await conn.execute(
                """INSERT INTO app.ingestion_jobs
                (id, document_id, kind, generation) VALUES (%s, %s, 'delete', -1)""",
                (uuid4(), uuid4()),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="lease_pair"):
            await conn.execute(
                """INSERT INTO app.vendor_permits (slot, token)
                                VALUES (1, %s)""",
                (uuid4(),),
            )
