"""Serialized deployment-only migrations for the LangGraph-owned schema."""

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from pydantic import SecretStr

CHECKPOINT_SCHEMA = "langgraph"
CHECKPOINT_MIGRATION_LOCK = "horizon-checkpoint-migration"
LOCK_WAIT_SECONDS = 60.0  # the same budget as the migration lock_timeout
LOCK_POLL_SECONDS = 0.2


class CheckpointSetupError(Exception):
    """The deployment connection does not have the configured migration identity."""


async def _acquire_migration_lock(conn: AsyncConnection[dict[str, object]]) -> None:
    """Poll for the lock rather than block on it.

    LangGraph's setup runs CREATE INDEX CONCURRENTLY, which waits for every older transaction.
    A second job blocked inside pg_advisory_lock is such a transaction while it waits for the
    holder, so a blocking acquire deadlocks two overlapping jobs.
    """
    async with asyncio.timeout(LOCK_WAIT_SECONDS):
        while True:
            cursor = await conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (CHECKPOINT_MIGRATION_LOCK,)
            )
            row = await cursor.fetchone()
            if row is not None and row["locked"]:
                return
            await asyncio.sleep(LOCK_POLL_SECONDS)


async def setup_checkpoints(*, dsn: SecretStr, expected_role: str) -> None:
    """Run library migrations once under a deployment lock; repeated calls are safe."""
    existing_options = conninfo_to_dict(dsn.get_secret_value()).get("options")
    if existing_options is not None and not isinstance(existing_options, str):
        raise CheckpointSetupError("Invalid checkpoint connection options")
    options = f"{existing_options or ''} -csearch_path=langgraph -clock_timeout=60000 -cstatement_timeout=60000"
    async with await AsyncConnection.connect(
        dsn.get_secret_value(),
        autocommit=True,
        row_factory=dict_row,
        options=options,
    ) as conn:
        cursor = await conn.execute("SELECT current_user AS role")
        row = await cursor.fetchone()
        if row is None or row["role"] != expected_role:
            raise CheckpointSetupError("Checkpoint setup requires the configured migration role")
        await _acquire_migration_lock(conn)
        try:
            schema = await conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s", (CHECKPOINT_SCHEMA,)
            )
            if await schema.fetchone() is None:
                await conn.execute("SELECT public.horizon_setup_checkpoint_schema()")
            await AsyncPostgresSaver(conn).setup()
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))", (CHECKPOINT_MIGRATION_LOCK,)
            )
