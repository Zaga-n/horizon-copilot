# Migration job

`services/migrations` is a one-shot job that brings the database schema to the version the runtime
images expect. It owns the single Alembic history for every table in schema `app` (chat and ingestion)
and the separate, library-managed LangGraph checkpoint schema `langgraph`. The runtime services never
run DDL and never import the migration package; the job needs PostgreSQL credentials only, with no
AWS, Google or API settings.

Package: [`services/migrations/src/horizon_migrations`](../../services/migrations/src/horizon_migrations).
Image: [`services/migrations/Dockerfile`](../../services/migrations/Dockerfile) (copies only the
migrations service and `libs/schema`).

## Commands

Entrypoint `python -m horizon_migrations.main [upgrade|check|sql]`, default `upgrade`. The image's
`ENTRYPOINT` is that module and `CMD` is `upgrade`, so `docker run <image> check` selects a subcommand.

| Command | Needs | Effect |
|---|---|---|
| `upgrade` | `MIGRATION_DATABASE_DSN`, `CHECKPOINT_MIGRATION_DSN` | Alembic `upgrade head` on the application schema, then LangGraph checkpoint setup. Prints "Application and checkpoint migrations completed." |
| `check` | `MIGRATION_DATABASE_DSN` | Succeeds only if the application schema is at head **and** matches the models (Alembic `check`). Makes no persistent change; Alembic prints its own "No new upgrade operations detected." before the summary line |
| `sql` | nothing, no connection | Prints the offline application upgrade SQL for review. It contains application DDL only: no extension, roles, schemas or checkpoint DDL |

Any failure exits with status 1 and prints `Migration job failed (<command>: <ExceptionType>); deployment
must stop.` without any connection string (an invalid subcommand fails earlier, in argument parsing, with status 2). Treat a non-zero exit as a deployment stop.

From the repository root (variables from the environment or `.env`; see
[Configuration](../reference/configuration.md#migration-job)):

```zsh
uv run --locked --package horizon-migrations python -m horizon_migrations.main upgrade
uv run --locked --package horizon-migrations python -m horizon_migrations.main check
uv run --locked --package horizon-migrations python -m horizon_migrations.main sql > upgrade.sql
```

For Alembic administration, use the service-owned configuration (there is no root `alembic.ini`):
`uv run --locked alembic -c services/migrations/alembic.ini …` (this path reads `MIGRATION_DATABASE_DSN` from the process environment only, not from `.env`, and needs the workspace installed with `uv sync --locked --all-packages`). The container does not use `alembic.ini`;
the script location is set in code.

## What `upgrade` does

1. **Application schema.** Alembic upgrades `app` to head inside one transaction (PostgreSQL
   transactional DDL). Before the revisions run it sets `lock_timeout = 60s` and takes a transaction
   advisory lock (`horizon-app-migration`), so overlapping jobs serialise. Autogenerate is scoped to
   schema `app` by [`db/migration_scope.py`](../../services/migrations/src/horizon_migrations/db/migration_scope.py);
   the version table is `app.alembic_version`.
2. **Checkpoint schema** ([`db/checkpoints.py`](../../services/migrations/src/horizon_migrations/db/checkpoints.py)):
   - connect with `search_path=langgraph` and 60 s lock and statement timeouts;
   - require `current_user` to equal `CHECKPOINT_MIGRATION_ROLE` (default `checkpoint_migrator`);
   - take the advisory lock `horizon-checkpoint-migration` by **polling** `pg_try_advisory_lock` every
     0.2 s for up to 60 s (a blocking lock could deadlock two overlapping jobs because LangGraph's
     setup uses `CREATE INDEX CONCURRENTLY`);
   - if schema `langgraph` does not exist, call `public.horizon_setup_checkpoint_schema()`, a
     `SECURITY DEFINER` helper from `provision.sql` that creates only that schema for
     `checkpoint_migrator`;
   - run LangGraph's `AsyncPostgresSaver.setup()`.

Repeating `upgrade` at head is safe: the version table guards the Alembic revisions and the checkpoint
DDL uses `IF NOT EXISTS`. Individual revision bodies are not idempotent.

## Revisions

| Revision | Adds |
|---|---|
| `20261002_0001` (baseline) | All chat tables and the document, version and chunk tables with their constraints and the HNSW index; three cyclic deferred foreign keys |
| `20261002_0002` (current head) | `vendor_permits`, `ingestion_jobs`, `upload_requests`; ingestion columns on chunks, versions and documents; relaxes `NOT NULL` on object references and file metadata; live-content unique index; backfills version metadata from documents |

**Downgrades are unsupported:** every revision's `downgrade()` raises "Baseline downgrade is destructive;
restore a database backup instead." Revision IDs follow `YYYYMMDD_NNNN`.

## What must happen around it

The job is one step of a fixed order ([Deployment](../guides/deployment.md),
[Local deployment](../guides/local-deployment.md)):

1. A platform administrator runs [`provision.sql`](../../services/migrations/sql/provision.sql): roles
   (`NOLOGIN`), the `vector` extension, schemas `app` and `langgraph`, and the helper function. No
   passwords live in it.
2. The migration job runs with credentials that act as `app_migrator` and `checkpoint_migrator`
   (distinct logins; neither can create objects in the other's schema).
3. A platform administrator runs [`runtime_grants.sql`](../../services/migrations/sql/runtime_grants.sql),
   which grants the runtime roles DML only. **Tables created by a new revision are unreadable by the
   runtime roles until this file (updated to name them) is applied again.**
4. The runtime services start. A runtime whose image revision differs from the database revision (older
   or newer) reports `/ready` 503 ([readiness](../reference/api.md#health-and-readiness)).

In local Compose, `postgres` runs the provisioning script at first volume initialisation,
`migrations` runs the job, `runtime-grants` runs `runtime_grants.sql` after it, and both APIs and the
worker wait for both to finish. `docker compose run --rm migrations` repeats only the job; the grants
run in the separate `runtime-grants` service.

## Adding a revision

1. Edit the tables in [`libs/schema/src/horizon_schema/models.py`](../../libs/schema/src/horizon_schema/models.py).
2. Generate against a database at the current head, with `MIGRATION_DATABASE_DSN` set:
   `uv run --locked alembic -c services/migrations/alembic.ini revision --autogenerate --rev-id YYYYMMDD_NNNN -m "<summary>"`.
   Review the output; Alembic autogenerate does not compare `CHECK` constraints, so write those by hand.
3. Set `SCHEMA_REVISION` in `models.py` to the new ID. Otherwise a contract test fails and every service
   reports not ready.
4. For a new table, add a handle in the owning service's `db/tables.py` and explicit grants in
   `runtime_grants.sql`; update `dev/stack/postgres/verify-roles.sql` if a role boundary changes.
5. Prefer additive changes: chat reads ingestion-owned tables and checks specific columns in readiness,
   and all three images embed the same `libs/schema`.
6. Add tests under `services/migrations/tests` and the service integration tests ([Testing](../guides/testing.md)).
7. A new workspace member needs a `COPY …/pyproject.toml` line in the migrations Dockerfile so
   `uv sync --locked` resolves the workspace.

This sequence was derived from the code and not executed during documentation.

## Testing

`services/migrations/tests/contract/test_migration_chain.py` (no database) asserts a single head equal to
`SCHEMA_REVISION`. `tests/integration/test_baseline.py` and `test_job.py` need a database; `test_job.py`
also runs the job in the real image when `MIGRATION_IMAGE` is set ([Testing](../guides/testing.md)). They
cover double runs, concurrent upgrades serialising on the lock, `check` failing on an unmigrated
database, `sql` output without a database, and failures not printing credentials.

## Implementation references

[`main.py`](../../services/migrations/src/horizon_migrations/main.py),
[`alembic/env.py`](../../services/migrations/src/horizon_migrations/alembic/env.py),
[`alembic/versions/`](../../services/migrations/src/horizon_migrations/alembic/versions),
[`db/checkpoints.py`](../../services/migrations/src/horizon_migrations/db/checkpoints.py).
