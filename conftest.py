"""Disposable per-test PostgreSQL databases replaying the actual migration."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parent
ALEMBIC_INI = REPO_ROOT / "services/migrations/alembic.ini"
PROVISION_SQL = (REPO_ROOT / "services/migrations/sql/provision.sql").read_text()


@pytest.fixture
async def migrated_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    dsn = os.environ.get("TEST_DATABASE_DSN")
    if not dsn:
        if os.environ.get("REQUIRE_INTEGRATION") == "1":
            pytest.fail("TEST_DATABASE_DSN is required by the integration job")
        pytest.skip("Set TEST_DATABASE_DSN to a disposable *_test database")
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
        pytest.fail("Refusing a target other than PostgreSQL with a *_test database")
    name = f"horizon_test_{uuid4().hex}"
    test_url = url.set(database=name)
    test_dsn = test_url.set(drivername="postgresql").render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with await psycopg.AsyncConnection.connect(test_dsn, autocommit=True) as conn:
                await conn.execute(PROVISION_SQL)
            monkeypatch.setenv(
                "MIGRATION_DATABASE_DSN",
                test_url.update_query_dict({"options": "-crole=app_migrator"}).render_as_string(
                    hide_password=False
                ),
            )
            config = Config(str(ALEMBIC_INI))
            await asyncio.to_thread(command.upgrade, config, "head")
            yield test_dsn
        finally:
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
