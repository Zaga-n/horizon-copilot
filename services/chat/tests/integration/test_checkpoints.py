"""Checkpoint migrations are repeatable and restricted to their own schema."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from horizon_chat.bootstrap.runtime import build_runtime
from horizon_chat.bootstrap.supervisor import ProcessHealth
from horizon_chat.config.secrets import Secrets
from horizon_chat.config.settings import Settings
from horizon_chat.db.readiness import DatabaseReadiness
from horizon_migrations.db.checkpoints import CheckpointSetupError, setup_checkpoints

pytestmark = pytest.mark.integration


async def test_setup_reruns_and_rejects_wrong_identity(migrated_database: str) -> None:
    with pytest.raises(CheckpointSetupError):
        await setup_checkpoints(dsn=SecretStr(migrated_database), expected_role="chat_runtime")
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as admin:
        await admin.execute("DROP SCHEMA langgraph")
    await setup_checkpoints(
        dsn=SecretStr(make_conninfo(migrated_database, options="-crole=checkpoint_migrator")),
        expected_role="checkpoint_migrator",
    )
    await setup_checkpoints(
        dsn=SecretStr(make_conninfo(migrated_database, options="-crole=checkpoint_migrator")),
        expected_role="checkpoint_migrator",
    )
    async with await psycopg.AsyncConnection.connect(migrated_database) as conn:
        cursor = await conn.execute(
            "SELECT schemaname FROM pg_tables WHERE tablename LIKE 'checkpoint%'"
        )
        schemas = {row[0] for row in await cursor.fetchall()}
        assert schemas == {"langgraph"}
        cursor = await conn.execute("SELECT max(v) FROM langgraph.checkpoint_migrations")
        row = await cursor.fetchone()
        assert row is not None and row[0] >= 0


async def test_runtime_readiness_requires_both_schemas(migrated_database: str) -> None:
    # Models build real clients from synthetic credentials and never contact AWS here.
    settings = Settings(
        environment_name="local",
        bind_host="127.0.0.1",
        bind_port=8080,
        frontend_origin="http://localhost:3000",
        aws_region="eu-west-1",
        main_model_id="global.openai.gpt-5.6-terra",
        utility_model_id="global.openai.gpt-5.6-luna",
        embedding_model_id="titan",
        google_client_id="test",
    )
    secrets = Secrets(
        database_dsn=migrated_database,
        checkpoint_database_dsn=migrated_database,
        aws_access_key_id=SecretStr("test"),
        aws_secret_access_key=SecretStr("test"),
    )
    async with build_runtime(settings, secrets, ProcessHealth()) as runtime:
        assert not await runtime.readiness.check()
        await setup_checkpoints(
            dsn=SecretStr(make_conninfo(migrated_database, options="-crole=checkpoint_migrator")),
            expected_role="checkpoint_migrator",
        )
        assert await runtime.readiness.check()
        async with await psycopg.AsyncConnection.connect(
            migrated_database, autocommit=True
        ) as conn:
            await conn.execute("ALTER TABLE app.document_chunks RENAME TO missing_chunks")
        assert not await runtime.readiness.check()
    assert isinstance(runtime.readiness.database, DatabaseReadiness)
    assert runtime.readiness.database.checkpoints.closed
    async with await psycopg.AsyncConnection.connect(migrated_database) as conn:
        cursor = await conn.execute("""SELECT count(*) FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid()""")
        assert await cursor.fetchone() == (0,)
