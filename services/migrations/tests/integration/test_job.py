"""Exercise the shipped job process, including its installed container artifact."""

import asyncio
import os
import sys

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from horizon_schema import SCHEMA_REVISION

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("container", [False, True])
async def test_job_replays_both_schemas_and_reruns(migrated_database: str, container: bool) -> None:
    image = os.environ.get("MIGRATION_IMAGE")
    if container and not image:
        pytest.skip("Set MIGRATION_IMAGE to the built migration container")
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute("DROP SCHEMA app CASCADE")
        await conn.execute("CREATE SCHEMA app AUTHORIZATION app_migrator")
    url = make_url(migrated_database)
    if container:
        url = url.set(host=os.environ.get("DOCKER_DATABASE_HOST", "host.docker.internal"))
    app_dsn = url.update_query_dict({"options": "-crole=app_migrator"}).render_as_string(
        hide_password=False
    )
    checkpoint_dsn = url.update_query_dict(
        {"options": "-crole=checkpoint_migrator"}
    ).render_as_string(hide_password=False)
    env = {
        **os.environ,
        "MIGRATION_DATABASE_DSN": app_dsn,
        "CHECKPOINT_MIGRATION_DSN": checkpoint_dsn,
    }
    args = [sys.executable, "-m", "horizon_migrations.main"]
    if container:
        assert image is not None
        args = [
            "docker",
            "run",
            "--rm",
            "--network=host",
            "-e",
            "MIGRATION_DATABASE_DSN",
            "-e",
            "CHECKPOINT_MIGRATION_DSN",
            image,
        ]
    for _ in range(2):
        process = await asyncio.create_subprocess_exec(
            *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, stderr.decode()
        assert b"Application and checkpoint migrations completed." in stdout
    async with await psycopg.AsyncConnection.connect(migrated_database) as conn:
        cursor = await conn.execute("SELECT version_num FROM app.alembic_version")
        assert await cursor.fetchone() == (SCHEMA_REVISION,)
        cursor = await conn.execute("SELECT max(v) FROM langgraph.checkpoint_migrations")
        assert await cursor.fetchone() == (len(AsyncPostgresSaver.MIGRATIONS) - 1,)
        cursor = await conn.execute("SELECT count(*) FROM app.documents")
        assert await cursor.fetchone() == (0,)


async def test_job_failure_stops_and_does_not_print_credentials() -> None:
    env = {
        **os.environ,
        "MIGRATION_DATABASE_DSN": "invalid://user:must-never-print-this@localhost/db",
        "CHECKPOINT_MIGRATION_DSN": "unused",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "horizon_migrations.main",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 1
    assert b"Migration job failed" in stderr
    assert b"must-never-print-this" not in stdout + stderr
    assert b"migrations completed" not in stdout


def role_dsn(database: str, role: str, *, host: str | None = None) -> str:
    url = make_url(database)
    if host is not None:
        url = url.set(host=host)
    return url.update_query_dict({"options": f"-crole={role}"}).render_as_string(
        hide_password=False
    )


async def run_job(*args: str, env: dict[str, str]) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode is not None
    return process.returncode, stdout, stderr


@pytest.mark.parametrize("container", [False, True])
async def test_check_passes_on_a_migrated_database(migrated_database: str, container: bool) -> None:
    image = os.environ.get("MIGRATION_IMAGE")
    if container and not image:
        pytest.skip("Set MIGRATION_IMAGE to the built migration container")
    host = os.environ.get("DOCKER_DATABASE_HOST", "host.docker.internal") if container else None
    env = {
        **os.environ,
        "MIGRATION_DATABASE_DSN": role_dsn(migrated_database, "app_migrator", host=host),
    }
    args = (
        ["docker", "run", "--rm", "--network=host", "-e", "MIGRATION_DATABASE_DSN", image, "check"]
        if container and image
        else [sys.executable, "-m", "horizon_migrations.main", "check"]
    )
    returncode, stdout, stderr = await run_job(*args, env=env)
    assert returncode == 0, stderr.decode()
    assert b"at head and matches the models" in stdout


async def test_check_fails_on_an_unmigrated_database(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute("DELETE FROM app.alembic_version")
    env = {**os.environ, "MIGRATION_DATABASE_DSN": role_dsn(migrated_database, "app_migrator")}
    returncode, _, stderr = await run_job(
        sys.executable, "-m", "horizon_migrations.main", "check", env=env
    )
    assert returncode == 1
    assert b"Migration job failed (check" in stderr


async def test_sql_prints_the_upgrade_without_a_database() -> None:
    env = {key: value for key, value in os.environ.items() if not key.endswith("_DSN")}
    returncode, stdout, stderr = await run_job(
        sys.executable, "-m", "horizon_migrations.main", "sql", env=env
    )
    assert returncode == 0, stderr.decode()
    assert b"CREATE TABLE app.documents" in stdout
    assert f"UPDATE app.alembic_version SET version_num='{SCHEMA_REVISION}'".encode() in stdout


async def test_concurrent_upgrades_serialize_on_the_migration_lock(migrated_database: str) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as admin:
        await admin.execute("DROP SCHEMA app CASCADE")
        await admin.execute("CREATE SCHEMA app AUTHORIZATION app_migrator")
        await admin.execute("SELECT pg_advisory_lock(hashtext('horizon-app-migration'))")
        env = {
            **os.environ,
            "MIGRATION_DATABASE_DSN": role_dsn(migrated_database, "app_migrator"),
            "CHECKPOINT_MIGRATION_DSN": role_dsn(migrated_database, "checkpoint_migrator"),
        }
        runs = [
            asyncio.create_task(run_job(sys.executable, "-m", "horizon_migrations.main", env=env))
            for _ in range(2)
        ]
        waiting = 0
        for _ in range(200):
            cursor = await admin.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            )
            row = await cursor.fetchone()
            waiting = row[0] if row else 0
            if waiting == 2 or any(run.done() for run in runs):
                break
            await asyncio.sleep(0.05)
        # Both runners wait on the lock: neither ran DDL while another holder had it.
        assert waiting == 2
        assert not any(run.done() for run in runs)
        await admin.execute("SELECT pg_advisory_unlock(hashtext('horizon-app-migration'))")
        results = await asyncio.gather(*runs)
    assert [returncode for returncode, _, _ in results] == [0, 0], [r[2] for r in results]
    async with await psycopg.AsyncConnection.connect(migrated_database) as conn:
        cursor = await conn.execute("SELECT version_num FROM app.alembic_version")
        assert await cursor.fetchone() == (SCHEMA_REVISION,)
