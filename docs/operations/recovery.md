# Recovery

What the system recovers by itself, what an operator can safely do, and what the repository does **not**
provide. The only backup and reset procedures defined are for the local Compose stack. No disaster-recovery
targets (RPO/RTO), production backup procedures, replication or tested restore exist in the repository.

## Recovered automatically

| Failure | Mechanism | Limits |
|---|---|---|
| Chat attempt abandoned (process or connection loss) | Lease expiry marks it `interrupted`; the user retries | Up to 150 s of `conversation_busy`; applied only when the conversation is next touched |
| Failure that could not be written during a database outage | In-process intent replayed by the reconciliation loop (every 5 s when healthy, backing off up to `maintenance_interval_seconds` while the database is down) and whenever that conversation's turn is submitted, retried or read | Lost if the process dies (then lease expiry applies); buffer holds 1024 intents |
| Worker crash or restart mid-job | Lease expiry (90 s) and a periodic scan resume unfinished chunks | A job that repeatedly kills its worker fails as `budget_exhausted` after 3 attempts |
| Lost `NOTIFY` | Next 2 s scan | |
| Transient Bedrock, storage or database errors | Bounded retries and backoff ([Ingestion](../services/ingestion.md#retry-budgets), [Chat](../services/chat.md#the-horizon-agent)) | Terminal categories are not retried automatically |
| Orphaned uploaded objects | Reconciliation removes unreferenced versions older than 10 minutes | |
| Expired conversations | Deleted by design after 30 days of inactivity | Not recoverable except from backup |

## Operator actions that are safe

- **Restart applications.** `docker compose restart chat ingestion ingestion-worker` runs no DDL. Accepted jobs
  resume without re-upload; in-flight chat attempts end as `cancelled` or `interrupted` and can be retried.
- **Retry a failed indexing job.** `POST /v1/ingestion-jobs/{id}:retry` re-queues it in place. Completed
  vectors are kept and only unfinished chunks are embedded again, so it does not duplicate work or paid
  embeddings for finished chunks. A job that is not `failed` is returned unchanged.
- **Restart a failed deletion.** Repeat `DELETE /v1/documents/{id}`: it re-queues the same cleanup job with
  no duplicate. The document stays excluded from retrieval meanwhile.
- **Retry a failed chat answer.** `POST …/turns/{id}:retry` with a fresh key and `expected_run_id`. It creates
  a new attempt; the old one is kept as history.
- **Re-run the migration job.** Safe at head ([Migration job](../services/migrations.md)).
- **Re-apply provisioning and grants.** `provision.sql` and `runtime_grants.sql` are re-runnable; re-apply
  `runtime_grants.sql` after any release that adds tables.

Do **not** hand-edit `ingestion_jobs`, `agent_runs` or lease columns: the retry and delete routes bump
generations, clear leases and reset counters consistently, and fencing relies on them. Do not replay model
execution to recreate an expired trace or to copy feedback into telemetry.

## Re-indexing documents

There is no bulk re-index tool. A document's content identity includes the pipeline fingerprint (parser
version, normalisation, window and overlap settings, embedding model), so:

- Uploading the **same bytes** with unchanged pipeline settings, by the same user and corpus, returns the existing job (409 if you target a different document).
- After a pipeline or embedding change, re-upload each document (with `document_id` to replace it in place);
  the new version publishes atomically and the old one is cleaned up.
- Changing the embedding model or dimension makes existing completed chunks incompatible with the
  configuration: chat `/ready` fails until they are replaced, and the dimension is also fixed by the
  `vector(1024)` column ([Data model](../reference/data-model.md)). Plan a migration and re-index; the
  repository has no procedure for it.

## Local backup and restore

Procedures for the Compose stack; they affect only this project. From the repository root:

**Back up (stop writers first).** `dev/backups/` is git-ignored.

```zsh
mkdir -p dev/backups
docker compose stop chat ingestion ingestion-worker
docker compose exec -T postgres pg_dump -U postgres -d horizon -Fc > dev/backups/horizon.dump
```

Then archive the MinIO volume to preserve exact object **version IDs** and server metadata (stop MinIO, archive its volume `minio-data`, whose Docker name is prefixed with the project name, `horizon-local_minio-data` by default; confirm with `docker volume ls`): PostgreSQL rows reference object versions, so a copy of only the latest objects
cannot restore them. For a consistent Langfuse backup, stop `langfuse-web` and `langfuse-worker` and back up its
PostgreSQL, ClickHouse, Redis and MinIO volumes together. Grafana dashboards are reproducible from the
checked-in JSON; telemetry in `lgtm-data` is disposable. Resume with `docker compose up -d`.

**Restore.** The repository documents the backup but **no restore commands**; the runbook only says to verify a
restoration in a separate Compose project. The dump is PostgreSQL custom format, which the standard `pg_restore` reads; `dev/stack/verification.md` records only `pg_restore --list` on such a dump, and no restore has been run. Restore the database and the matching
MinIO volume **together** into a fresh project, run `docker compose up -d` (migrations and grants repeat safely),
and verify with `/ready` and the [smoke test](../guides/smoke-test.md).

**Reset (destructive).** Back up first, then follow [Local deployment → Reset](../guides/local-deployment.md#reset).

Before any schema change or reset, stop the runtime writers and take the database backup and the original
objects including versions.

## Production

Not defined: backup schedule, retention, point-in-time recovery, cross-region copies, restore drills, or
ownership. The migration job has no downgrade. Rolling back an application image after a schema change needs
the matching database state ([Deployment → Rollback](../guides/deployment.md#rollback)). Citation availability
is computed from database state (`object_key` present, version live), not by probing the bucket, so losing
object-store data is not reflected in history until rows are changed.

## Verifying recovery

- `/ready` on both APIs, and `worker-health` on the worker.
- `select status, count(*) from app.ingestion_jobs group by 1;` shows no unexpected `failed` or stuck `processing`
  rows after the lease intervals have passed.
- A smoke upload and question succeed ([Smoke test](../guides/smoke-test.md)).
