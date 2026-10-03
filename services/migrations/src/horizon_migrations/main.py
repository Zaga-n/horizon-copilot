"""One-shot migration job for the database shared by chat and ingestion.

`upgrade` (the default) applies both schemas; `check` verifies the application schema is at
head with no model drift; `sql` prints the application upgrade SQL without connecting.
"""

import argparse
import asyncio
import sys
from enum import StrEnum

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from horizon_migrations.db.checkpoints import setup_checkpoints


class Command(StrEnum):
    UPGRADE = "upgrade"
    CHECK = "check"
    SQL = "sql"


class AppMigrationSettings(BaseSettings):
    """The application schema connection only; runtime and AWS settings are not needed."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_database_dsn: SecretStr


class MigrationSettings(AppMigrationSettings):
    """Both migration connections, for the full upgrade."""

    checkpoint_migration_dsn: SecretStr
    checkpoint_migration_role: str = "checkpoint_migrator"


def alembic_config(*, dsn: SecretStr | None) -> Config:
    config = Config()
    config.set_main_option("script_location", "horizon_migrations:alembic")
    if dsn is not None:
        config.attributes["migration_database_dsn"] = dsn.get_secret_value()
    return config


def run_migrations(settings: MigrationSettings) -> None:
    command.upgrade(alembic_config(dsn=settings.migration_database_dsn), "head")
    asyncio.run(
        setup_checkpoints(
            dsn=settings.checkpoint_migration_dsn,
            expected_role=settings.checkpoint_migration_role,
        )
    )


def check_migrations(settings: AppMigrationSettings) -> None:
    """Raise unless the application schema is at head and matches the models."""
    command.check(alembic_config(dsn=settings.migration_database_dsn))


def print_migration_sql() -> None:
    """Write the offline application upgrade script to stdout, for review by a platform admin."""
    command.upgrade(alembic_config(dsn=None), "head", sql=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="horizon_migrations")
    parser.add_argument("command", nargs="?", choices=list(Command), default=Command.UPGRADE)
    selected = Command(parser.parse_args(argv).command)
    try:
        if selected == Command.UPGRADE:
            run_migrations(MigrationSettings())
        elif selected == Command.CHECK:
            check_migrations(AppMigrationSettings())
        else:
            print_migration_sql()
    except Exception as exc:  # noqa: BLE001 CLI boundary: exit nonzero without printing DSNs/secrets.
        sys.stderr.write(
            f"Migration job failed ({selected}: {type(exc).__name__}); deployment must stop.\n"
        )
        raise SystemExit(1) from None
    if selected == Command.UPGRADE:
        sys.stdout.write("Application and checkpoint migrations completed.\n")
    elif selected == Command.CHECK:
        sys.stdout.write("Application schema is at head and matches the models.\n")


if __name__ == "__main__":
    main()
