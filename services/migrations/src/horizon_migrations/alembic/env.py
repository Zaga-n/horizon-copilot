"""Serialized async migrations restricted to app; checkpoint DDL is independent."""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_migrations.db.migration_scope import include_name
from horizon_schema import metadata

config = context.config


def migrate(connection: Connection) -> None:
    # Schema ownership is provisioned by the platform before this command.
    context.configure(
        connection=connection,
        target_metadata=metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="app",
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        connection.execute(text("SET LOCAL lock_timeout = '60s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('horizon-app-migration'))"))
        context.run_migrations()


async def online() -> None:
    dsn = config.attributes.get("migration_database_dsn") or os.environ.get(
        "MIGRATION_DATABASE_DSN"
    )
    if not isinstance(dsn, str) or not dsn:
        raise ValueError("MIGRATION_DATABASE_DSN is required")
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        target_metadata=metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="app",
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
