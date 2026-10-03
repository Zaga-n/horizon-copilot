# Troubleshooting

Symptom-driven diagnosis for the local stack and, where the same code applies, deployed environments.
Every scenario is **derived from implementation and tests, not from recorded incidents**: the repository
holds no incident history or on-call ownership. Database queries use the local Compose administrator
(`docker compose exec -T postgres psql -U postgres -d horizon -c "…"`); in a deployed environment use
your platform's equivalent read access. Signals are described in [Observability](observability.md).

First look, for any problem:

```zsh
docker compose ps -a                                   # states of every container, one-shot jobs included
docker compose logs --tail=100 chat ingestion ingestion-worker
curl -sS http://localhost:8080/ready; echo             # {"status":"ready"} or {"status":"unready"}
curl -sS http://localhost:8081/ready; echo
```

Application logs are JSON and deliberately content-free: they carry event names, IDs, categories and the
exception **class**, never messages, so some root causes (notably AWS error codes) cannot be read from them.

## The stack will not start

**`docker compose config` or `up` fails with "set GOOGLE_CLIENT_ID" (or `MAIN_MODEL_ID` /
`UTILITY_MODEL_ID`).** A required variable is missing from `.env`. Copy `.env.example`, set it, and rerun
`docker compose config --quiet`.

**`migrations` exits non-zero.** Every runtime depends on it, so nothing else starts.

- Signal: `docker compose logs migrations` shows `Migration job failed (upgrade: <ExceptionType>); deployment
  must stop.` by design the message hides the cause and any credentials.
- Likely causes from the code: the migration logins cannot connect (for example a password variable changed
  after the volume was initialised, because roles keep the password from first initialisation); the
  checkpoint connection's `current_user` is not `checkpoint_migrator` ("Checkpoint setup requires the
  configured migration role"); a 60 s lock or statement timeout while another job holds the migration lock;
  the roles or extension were never provisioned (`provision.sql` runs only on a fresh data volume).
- Diagnose: `docker compose logs postgres` for server-side errors; confirm the roles exist with
  `docker compose exec -T postgres psql -U postgres -d horizon -c "\du"`.
- Recover: fix the cause, then `docker compose up -d`. Repeating the job is safe. If the volume was created
  with other passwords, back up and [reset](../guides/local-deployment.md#reset) rather than editing roles
  by hand.
- Verify: `docker compose ps -a` shows `migrations` and `runtime-grants` exited 0; both `/ready` succeed.

**A port is already in use.** Published host ports: 3000, 8080, 8081, 5432, 9000, 9001, 4318, 13133, 3001,
3002, 19090. The configurable ones are listed in [Configuration → Compose inputs](../reference/configuration.md#compose-inputs);
the API and frontend ports are literals in `compose.yaml`.

## `/ready` returns 503 (`{"status":"unready"}`)

Neither service calls AWS, Google or MinIO in `/ready`, so the cause is the database or a loop.

| Check (chat) | Query or action | Expected |
|---|---|---|
| Schema revision | `select version_num from app.alembic_version;` | `20261002_0002` (the value compiled into the image, `SCHEMA_REVISION`) |
| Vector column | `select format_type(atttypid, atttypmod) from pg_attribute where attrelid = 'app.document_chunks'::regclass and attname = 'embedding';` | `vector(1024)` |
| Checkpoint schema | `select max(v) from langgraph.checkpoint_migrations;` | 9 for the pinned LangGraph library |
| Published chunks compatible | Embedding model/dimension equal to configuration, no null embeddings or blank text on live published chunks | none incompatible |
| Background loops | Logs for `loop_crashed` | none; a crashed loop also makes `/health` 503 |

Ingestion checks only the revision and the configured dimension (1024).

- **Revision mismatch** (older *or* newer than the image): the migration job has not run, or an image built
  from different code is running. Run the job, or redeploy matching images ([Deployment](../guides/deployment.md)).
- **Permission errors** (readiness reads tables): `runtime_grants.sql` was not applied after the migration.
  In Compose, `docker compose up -d` repeats `runtime-grants`.
- **Timeout**: the check is bounded by `readiness_timeout_seconds` (5 s); a saturated pool or slow database
  also yields 503.
- A chat process whose database was unreachable at *start* exits instead of reporting unready.

## Authentication and browser errors

**401 `invalid_identity`.** The token is missing, expired, oversize, forged, an *access* token, or has the wrong
**audience**. Confirm `GOOGLE_CLIENT_ID` is identical for chat, ingestion (`INGESTION_GOOGLE_CLIENT_ID`) and the
frontend (`FRONTEND_GOOGLE_CLIENT_ID`), and that the token is an ID token for that client. Tokens are short
lived and are not refreshed: sign in again ([Authentication](../guides/authentication.md)).

**503 `identity_unavailable` (chat) or `service_unavailable` (ingestion) on every request.** Google's signing
certificates cannot be fetched (each verification is expected to fetch them): check outbound HTTPS from the
API container.

**Browser "blocked by CORS", or preflight 400.** The page origin differs from `FRONTEND_ORIGIN` /
`INGESTION_FRONTEND_ORIGIN`. They must match exactly (scheme, host, port, no trailing slash); the shipped
value is `http://localhost:3000`. Also add the origin to the Google client's authorized origins.

**Blank page.** The frontend `config.js` is missing or invalid ([Frontend](../services/frontend.md#runtime-configuration)).

**"Session expired" repeatedly.** Expected after the token's lifetime; there is no refresh.

## Chat turns

**409 `conversation_busy`.** Another attempt holds the conversation lease (150 s, not renewed), including an
attempt whose process or connection died. It clears itself: the lease expires, then the attempt becomes
`interrupted` the next time the conversation is touched. Inspect with
`select id, status, active_run_id, lease_until from app.conversations where id = '<conversation-uuid>';`.

**The stream ends without `completed`/`failed`, or `failed` arrives.** Read the saved state first
(`GET …/turns/{turn_id}`), then act by category ([Events](../reference/events.md#chat-turn-stream-sse)):

| Category | Likely cause | Action |
|---|---|---|
| `budget_exhausted` | The 120 s deadline or a call budget was hit | Retry; if systematic, inspect latency and budgets ([Chat](../services/chat.md#the-horizon-agent)) |
| `service_unavailable` | Bedrock or the database was unavailable; **also IAM or model-access denials and an unknown or unsupported model ID** (a `ValidationException` naming a model identifier), which are classified as unavailable | Check the database; verify AWS credentials and model access outside the app (see below) |
| `provider_rejected` | Bedrock rejected the request (a `ValidationException` that does not name a model ID) | Check `MAIN_MODEL_ID`, `UTILITY_MODEL_ID`, reasoning-effort and token settings |
| `provider_protocol` | Unusable provider response | Retry; if persistent, inspect the models and library versions |
| `invalid_citations` | The answer cited an unknown marker, or none when evidence was required; also an empty answer or malformed decision or final-phase output | Retry (model behaviour); partial text is kept on the failed message |
| `index_integrity` | The query vector handed to the index was not 1024 finite numbers (a defensive check; corrupt chunk rows are logged as `retrieval_row_corrupt` and skipped instead) | Check the embedding model and dimension configuration and [readiness](../reference/api.md#health-and-readiness) |
| `interrupted` | Process loss or lease expiry | Retry |
| `cancelled` | Client disconnected | Retry if wanted |
| `internal` | Unclassified | Look for `turn_failed` logs with `trace_id` |

`persistence_pending=true` means the database could not record the failure: do **not** retry; wait for the
database and read the turn, which also triggers repair ([Chat](../services/chat.md#failure-handling-and-recovery)).
To see attempts of a conversation:

```sql
select id, attempt_number, status, failure_category, started_at, ended_at
from app.agent_runs where conversation_id = '<conversation-uuid>' order by started_at;
```

**Retry returns 409 `stale_retry`.** Only the newest, failed attempt of the turn can be retried, with
`expected_run_id` set to that attempt's run ID; read the turn to get it.

**Text arrives in one lump instead of streaming.** A proxy is buffering or compressing the response. The
chat API sends `X-Accel-Buffering: no`; disable buffering and compression for `POST …:stream` at every hop.

**The answer says nothing about the documents / "no evidence".** The index may be empty, the document not
`ready`, or it belongs to another Google account (documents are private to the uploader). Check
`GET /v1/documents` as the same account.

**AWS errors cannot be read from the logs.** No log or span field exposes the AWS error code or message.
Verify access with the same credentials outside the application (for example the AWS CLI against the same
region and model IDs). Not determined from the repository: any in-product diagnostic for this.

## Documents and ingestion

**Upload rejected.** The 422 codes ([API reference](../reference/api.md#ingestion-api)):
`unsupported_file` (not `.pdf`/`.docx`), `file_type_mismatch` (content type must match exactly),
`empty_file`, `malformed_file`, `parser_timeout`, `parser_failed`, `invalid_filename`, `invalid_upload_metadata`, `invalid_document_id`, `file_and_metadata_required`. Others: 413 `file_too_large` (50 MiB), 408
`upload_timeout` (120 s), 503 `service_unavailable` (the response never names the cause; internal causes include a bucket that is not versioned, `versioned_bucket_required`, and a commit that took too long, `upload_finalization_expired`, and neither appears in clients or logs). **409 `request_conflict` hides its
reason**; the possibilities are: the idempotency key was reused with different content or metadata, the
content already exists under another document, the target document is not live, or a replacement is already
in progress.

**Document stays `queued`.** The job has not been claimed: the worker is not running or healthy, or all `worker_concurrency` (4) slots are busy with earlier jobs. Run
`docker compose ps ingestion-worker` and
`docker compose exec ingestion-worker python -m horizon_ingestion.main worker-health`. The worker finds due
work by scanning every 2 s even if notifications are lost, so a persistent `queued` job with idle slots means no live worker, or a worker that exited at start because its readiness conditions failed. Inspect:

```sql
select id, kind, status, stage, attempts, cycle_attempts, error_category, next_retry_at, lease_until
from app.ingestion_jobs order by updated_at desc limit 20;
```

**Document ends `failed`.** Act on `error_category`:

| Category | Meaning | Action |
|---|---|---|
| `no_extractable_text` | No text (for example an image-only PDF; OCR is not supported) | Upload a text-based file |
| `extraction_failed` | Corrupt, encrypted or oversize input | Fix the file |
| `provider_rejected`, `integrity_failed`, `budget_exhausted` | Terminal at once | Fix configuration or file; retry once corrected |
| `storage_unavailable`, `provider_unavailable`, `provider_protocol`, `manifest_incomplete`, `internal` | Transient; retried automatically up to 3 attempts, then failed | Fix the dependency, then `POST /v1/ingestion-jobs/{id}:retry` (re-embeds only unfinished chunks) |

**Retry returns 409.** The document is deleting, its version is retired, the job is not an index job, or
another index job is active.

**Document stuck in `deleting`.** The document is already excluded from retrieval; cleanup has not finished. Two cases. (1) The delete job is `retrying`: every storage error, including an access denial on removal, is retried indefinitely, so fix the cause (for example the ingestion account's bucket permissions) and cleanup completes by itself; see the `ingestion_job_retry` logs and `select id, status, error_category, next_retry_at from app.ingestion_jobs where kind = 'delete' and status = 'retrying';`. (2) The job is `failed` (an integrity-class or budget fault after 3 attempts): find these with the `stuck-deletions` query in [`services/ingestion/README.md`](../../services/ingestion/README.md), fix the cause, then repeat `DELETE /v1/documents/{id}`, which restarts the same cleanup job. A failed `superseded_cleanup` job has no API recovery ([Ingestion](../services/ingestion.md#known-limitations)).

**Worker unhealthy.** `worker-health` fails when the health file is absent or stale (> 27 s with defaults), a
required loop crashed, or the dispatch loop cannot reach the database. A disconnected LISTEN alone does not
fail it. Check database connectivity and worker logs for `ingestion_loop_crashed` and
`ingestion_listener_reconnecting`. Restart with `docker compose restart ingestion-worker`; accepted jobs resume
without re-uploading.

**Orphaned objects under `attempts/`.** Created when an upload failed between storing and committing. They are
removed automatically once unreferenced and older than 10 minutes; nothing to do.

## Telemetry

**No traces or metrics in Grafana.** Check `OTLP_ENDPOINT` / `INGESTION_OTLP_ENDPOINT` is set (unset disables
export), `curl -fsS http://localhost:13133/` for the Collector, and remember the Collector drops attributes
outside its allowlist ([Observability](observability.md)). A missing trace does not mean the request did not
happen. Chat turn failures do not appear in the "Request errors" panel (HTTP stays 200); query the agent boundary metric (`outcome="error"` also includes cancelled turns) or search logs for `turn_failed`.

## Tests

**Playwright cannot start a server on 3002.** The Compose stack publishes Langfuse on 3002 by default; run
`FRONTEND_TEST_PORT=3003 npm run test:browser` ([Testing](../guides/testing.md#frontend)).

**Integration tests skip or "Refusing a target other than PostgreSQL with a *_test database".** Set `TEST_DATABASE_DSN` to a disposable database whose name ends in `_test`; add `REQUIRE_INTEGRATION=1` to fail instead of skipping when it is missing.

## Data that disappears

Conversations vanish after `retention_days` (30) of inactivity, by design; documents and original files do not
expire. A conversation in `purging` state is invisible to the API.
