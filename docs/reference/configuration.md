# Configuration reference

How each deployable is configured, what is required, and every supported setting. Behaviour was
derived from the settings classes, not only from the `.env.example` files:
[chat](../../services/chat/src/horizon_chat/config/settings.py),
[ingestion](../../services/ingestion/src/horizon_ingestion/config/settings.py),
[migrations](../../services/migrations/src/horizon_migrations/main.py).

## Essentials

To run the full local stack (see [Local deployment](../guides/local-deployment.md)) you set only
these, in the repository-root `.env` (copy [`.env.example`](../../.env.example)):

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_ID` | yes | Public Google OAuth web client ID. Both APIs verify ID tokens issued for it, and the frontend uses it to sign in |
| `MAIN_MODEL_ID` | yes | Bedrock inference-profile ID for the main (decision and final) chat model |
| `UTILITY_MODEL_ID` | yes | Bedrock inference-profile ID for the utility model (guardrail, summary, rewrite) |
| `AWS_REGION` | no (default `eu-west-1`) | Region for Bedrock |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | no | Static credentials for Bedrock, supplied to both chat and ingestion. Must be given together |
| `AWS_SESSION_TOKEN` | no | Only for temporary credentials; requires the pair above |

Compose supplies every other value. If both AWS key variables are empty, the AWS SDK's default
credential chain is used inside the containers; Compose does not mount any credential files or pass
`AWS_PROFILE`, so using a login profile needs a Compose override. Empty variables count as unset
(`env_ignore_empty`).

## How configuration is resolved

Applies to chat and ingestion:

1. **Deployment inputs** come from environment variables (chat: no prefix, e.g. `BIND_PORT`;
   ingestion: prefix `INGESTION_`, e.g. `INGESTION_BIND_PORT`). Names are case-insensitive. A `.env`
   file in the working directory is read as well.
2. **Policy** (limits, timeouts, defaults) comes from layered YAML in the config directory,
   deep-merged in this order, later layers overriding earlier ones:
   [`config/base.yaml`](../../config/base.yaml) →
   `config/<environment>.yaml` ([`local`](../../config/local.yaml), [`staging`](../../config/staging.yaml),
   [`production`](../../config/production.yaml)) → `config/services/<service>.yaml` →
   `config/services/<service>.<environment>.yaml` (optional, none exist). `base` and the environment
   file are required.
3. **Precedence** (highest first): programmatic init → process environment → `.env` → YAML policy.
   An environment variable therefore overrides YAML. Any policy key can be overridden by its
   environment variable.
4. **Strictness.** YAML may only contain policy keys. A key that is unknown, or that belongs to the
   *environment-only* set below, aborts start-up with `YAML keys are not policy fields`.
   Optional fields (`otlp_endpoint`, `service_instance_id`) are also not YAML-settable.
5. **Environment selection.** `environment_name` (`local`, `staging` or `production`) must come from the
   environment or `.env`; it selects the YAML layer.
6. **Config directory.** `HORIZON_CONFIG_DIR` (read from the real process environment only, not from
   `.env`) names the directory; otherwise the nearest ancestor `config/` containing `base.yaml` is
   used. The container images set `HORIZON_CONFIG_DIR=/app/config`.
7. **Reload.** Settings are loaded once at process start and are immutable. Any change needs a
   restart. Invalid combinations fail start-up before any dependency is opened.

Secrets (database DSNs, AWS keys, MinIO keys) are loaded separately as masked values and are never
logged. YAML files hold no secrets.

**Environment-only fields** (not YAML-settable): `environment_name`, `bind_host`, `bind_port`,
`frontend_origin`, `aws_region`, `google_client_id`, the model IDs, and for ingestion `minio_endpoint`
and `minio_bucket`.

## Chat service

Source: [`config/settings.py`](../../services/chat/src/horizon_chat/config/settings.py),
[`config/secrets.py`](../../services/chat/src/horizon_chat/config/secrets.py). Example:
[`services/chat/.env.example`](../../services/chat/.env.example). Chat variables have **no prefix**.

### Required deployment inputs

| Env variable | Type / constraint | Secret | Purpose and consumer |
|---|---|---|---|
| `ENVIRONMENT_NAME` | `local` \| `staging` \| `production` | no | Selects the YAML layer; gates local identity |
| `BIND_HOST` | non-empty | no | Listen address passed to uvicorn |
| `BIND_PORT` | 1–65535 | no | Listen port |
| `FRONTEND_ORIGIN` | bare `http(s)` origin: no path, query, fragment, credentials or trailing slash; `https` in production | no | The only CORS origin |
| `AWS_REGION` | non-empty | no | Bedrock client region |
| `MAIN_MODEL_ID` | non-empty | no | Decision and final chat model |
| `UTILITY_MODEL_ID` | non-empty | no | Guardrail, summary and rewrite model |
| `EMBEDDING_MODEL_ID` | non-empty | no | Query embedding model (`amazon.titan-embed-text-v2:0` in every shipped example) |
| `GOOGLE_CLIENT_ID` | non-empty | no (public) | Expected ID-token audience |
| `DATABASE_DSN` | `postgresql` or `postgresql+psycopg` URL with host and database | **yes** | Application pool (SQLAlchemy). Use the `chat_runtime` role, never a migration role |
| `CHECKPOINT_DATABASE_DSN` | PostgreSQL URL with host and database; use plain `postgresql://` (it is handed to libpq unchanged, so a `postgresql+psycopg://` URL passes validation but is expected to fail when the pool opens) | **yes** | LangGraph checkpoint pool |

### Optional inputs and selectors

| Env variable | Default | Purpose |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | unset (SDK credential chain) | Static Bedrock credentials; both or neither. Secret |
| `AWS_SESSION_TOKEN` | unset | Needs the pair above. Secret |
| `OTLP_ENDPOINT` | unset (export off) | Collector base HTTP URL; `/v1/traces` and `/v1/metrics` are appended |
| `SERVICE_INSTANCE_ID` | random UUID per start | OpenTelemetry `service.instance.id` |
| `HORIZON_CONFIG_DIR` | nearest `config/` | Policy directory |

### Policy settings (YAML key = lower-case env name)

Defaults come from [`config/base.yaml`](../../config/base.yaml) and
[`config/services/chat.yaml`](../../config/services/chat.yaml).

| Key | Type / constraint | Default | Purpose and consumer |
|---|---|---|---|
| `identity_mode` | `google` \| `local` | `google` (`config/local.yaml`: `local`) | Token verification mode; `local` requires `environment_name=local` and a loopback `bind_host` |
| `local_subject` | non-empty | `local-horizon-user` | Owner key in local mode |
| `embedding_dimensions` | must be 1024 | 1024 | Vector size check; must match the `vector(1024)` column |
| `max_model_calls` | int > 0 | 5 | Graph model-call limit |
| `max_tool_calls` | 1–2 | 2 | Searches per attempt |
| `max_rewrite_calls` | int > 0 | 2 | Query rewrites per attempt |
| `max_physical_model_attempts` | int > 0, must exceed `max_model_calls` | 24 | Every Bedrock request incl. utility calls, retries, embedding |
| `max_output_tokens` | int > 0 | 4096 | `max_tokens` on all chat models |
| `main_reasoning_effort` | `none` \| `low` \| `medium` \| `high` | `low` | Decision and final models |
| `utility_reasoning_effort` | same | `none` | Utility model |
| `max_chunks` | 1–8 | 8 | Retrieval result cap |
| `max_excerpt_chars` | int > 0 | 3000 | Per-chunk excerpt |
| `max_evidence_chars` | int > 0 | 24000 | Evidence per attempt |
| `turn_deadline_seconds` | float > 0 | 120 | Graph deadline (excludes admission) |
| `turn_lease_seconds` | float > 0, must exceed the deadline | 150 | Conversation lease for an active attempt |
| `retry_attempts` | int > 0 | 3 | Total tries for model, tool, summary and failure-persistence retries |
| `retry_initial_backoff_seconds` | float > 0, ≤ max | 0.5 | Backoff start |
| `retry_max_backoff_seconds` | float > 0 | 4 | Backoff cap |
| `summary_trigger_tokens` | int > 0 | 12000 | History size that triggers a summary |
| `summary_keep_messages` | int > 0 | 8 | Recent messages kept after summary |
| `retention_days` | int > 0 | 30 | Inactivity before a conversation is purged |
| `maintenance_batch_size` | int > 0 | 100 | Conversations per retention pass |
| `maintenance_interval_seconds` | float > 0 | 3600 | Retention cadence **and** backoff cap for both loops (Compose sets 86400) |
| `recovery_interval_seconds` | float > 0 | 5 | Failure-reconciliation cadence |
| `maintenance_shutdown_grace_seconds` | float > 0 | 5 | Loop drain at shutdown (must fit the deployment stop grace) |
| `database_pool_size` | int > 0 | 10 | Pool size for both pools (`max_overflow=0`) |
| `database_connect_timeout_seconds` | int > 0 | 5 | Database connect timeout |
| `database_statement_timeout_ms` | int > 0 | 10000 | `statement_timeout` on both pools |
| `provider_connect_timeout_seconds` | float > 0 | 5 | Bedrock connect timeout |
| `provider_read_timeout_seconds` | float > 0 | 30 | Bedrock read timeout |
| `readiness_timeout_seconds` | float > 0 | 5 | `/ready` bound |
| `log_level` | `DEBUG`…`CRITICAL` | `INFO` | Root log level |
| `log_full_exception_trace` | bool | `false` | Adds file/function/line frames to error logs |

A word search found a reference outside the settings module for every field above, so none is an
obviously unwired knob. Retry, deadline and budget semantics: [chat service](../services/chat.md).

## Ingestion service

Source: [`config/settings.py`](../../services/ingestion/src/horizon_ingestion/config/settings.py),
[`config/secrets.py`](../../services/ingestion/src/horizon_ingestion/config/secrets.py). Example:
[`services/ingestion/.env.example`](../../services/ingestion/.env.example). Every variable except `HORIZON_CONFIG_DIR` is prefixed `INGESTION_`. The API and the worker read the same settings.

### Required deployment inputs

| Env variable | Type / constraint | Secret | Purpose |
|---|---|---|---|
| `INGESTION_ENVIRONMENT_NAME` | `local` \| `staging` \| `production` | no | Selects the YAML layer |
| `INGESTION_BIND_HOST`, `INGESTION_BIND_PORT` | non-empty; 1–65535 | no | Required by both processes; only the API listens on them (the host is also checked for loopback in local identity mode) |
| `INGESTION_FRONTEND_ORIGIN` | bare origin as for chat | no | CORS origin |
| `INGESTION_GOOGLE_CLIENT_ID` | non-empty | no | Expected ID-token audience |
| `INGESTION_AWS_REGION` | non-empty | no | Titan embedding region (also the MinIO S3 client's region name) |
| `INGESTION_EMBEDDING_MODEL_ID` | only `amazon.titan-embed-text-v2:0` | no | Embedding model |
| `INGESTION_MINIO_ENDPOINT` | `http(s)` URL | no | S3-compatible endpoint |
| `INGESTION_MINIO_BUCKET` | non-empty | no | Bucket (must be versioned; the shipped policy covers `horizon-documents`) |
| `INGESTION_DATABASE_DSN` | PostgreSQL URL | **yes** | Use a login that acts as `ingestion_runtime` |
| `INGESTION_MINIO_ACCESS_KEY`, `INGESTION_MINIO_SECRET_KEY` | non-empty | **yes** | Bucket credentials, independent of AWS credentials |

### Optional inputs

| Env variable | Default | Purpose |
|---|---|---|
| `INGESTION_AWS_ACCESS_KEY_ID`, `INGESTION_AWS_SECRET_ACCESS_KEY` | unset (SDK chain) | Static Bedrock credentials; both or neither. Secret |
| `INGESTION_AWS_SESSION_TOKEN` | unset | Needs the pair. Secret |
| `INGESTION_OTLP_ENDPOINT` | unset | Collector base URL |
| `INGESTION_SERVICE_INSTANCE_ID` | random UUID | `service.instance.id`; also the worker's **lease owner**, so give each replica its own value |
| `HORIZON_CONFIG_DIR` (unprefixed) | nearest `config/` | Policy directory |

### Policy settings

Defaults from [`config/base.yaml`](../../config/base.yaml) and
[`config/services/ingestion.yaml`](../../config/services/ingestion.yaml).

| Key | Type / constraint | Default | Purpose |
|---|---|---|---|
| `identity_mode`, `local_subject` | as chat | `google`, `local-horizon-user` | Identity mode and local owner |
| `embedding_dimensions` | must be 1024 | 1024 | Vector size |
| `database_pool_size` | int > 0 | 10 | Pool size |
| `database_connect_timeout_seconds` | int > 0 | 5 | Connect timeout |
| `database_statement_timeout_ms` | int > 0 | 10000 | Statement timeout |
| `provider_connect_timeout_seconds` / `provider_read_timeout_seconds` | float > 0 | 5 / 30 | Timeouts for Bedrock, the MinIO S3 client and the Google certificate fetch; SDK retries are disabled for Bedrock and S3 |
| `readiness_timeout_seconds` | float > 0 | 5 | `/ready` bound; the worker's start-up readiness check and listener wait |
| `log_level`, `log_full_exception_trace` | as chat | `INFO`, `false` | Logging |
| `max_upload_bytes` | int > 0 | 52428800 | File size limit (request body cap is this plus 64 KiB) |
| `upload_timeout_seconds` | float > 0 | 120 | Whole-upload deadline |
| `max_extracted_chars` | int > 0 | 10000000 | Extracted text cap |
| `max_extraction_units` | int > 0 | 5000 | Pages (PDF) / blocks bound |
| `extraction_timeout_seconds` | float > 0 | 60 | Parser wall time |
| `parser_memory_bytes` | int > 0 | 1073741824 | Parser address-space limit (Linux only) |
| `chunk_timeout_seconds` | float > 0, < job timeout | 120 | Provider time per chunk |
| `job_timeout_seconds` | float > 0 | 3600 | Job time budget (waiting excluded) |
| `window_size` | int > 0 | 2000 | Chunk window (code points) |
| `overlap` | int ≥ 0, < `window_size` | 200 | Window overlap |
| `worker_concurrency` | int > 0 | 4 | Concurrent jobs per worker process |
| `vendor_concurrency` | int > 0 | 4 | Concurrent embedding calls across all workers |
| `scan_interval_seconds` | float > 0 | 2 | Due-work scan cadence; also listener reconnect base |
| `job_lease_seconds` | float > 0, > 3 × heartbeat | 90 | Job lease |
| `heartbeat_interval_seconds` | float > 0 | 20 | Lease renewal |
| `permit_lease_seconds` | float > 0, > provider connect + read | 45 | Vendor permit lease |
| `max_call_attempts` | int > 0, ≤ `max_chunk_attempts` | 3 | Physical calls per chunk per attempt |
| `max_chunk_attempts` | int > 0 | 9 | Physical calls per chunk per retry cycle |
| `max_job_attempts` | int > 0 | 3 | Attempts per retry cycle |
| `retry_initial_backoff_seconds` | float > 0, ≤ max | 0.5 | Backoff start |
| `retry_max_backoff_seconds` | float > 0 | 10 | Backoff cap; also caps the LISTEN reconnect backoff and enters the worker-health scan-age bound |
| `orphan_grace_seconds` | float > 0, > `upload_timeout_seconds` | 600 | Minimum age of an unreferenced object before removal |
| `reconciliation_interval_seconds` | float > 0 | 60 | Orphan scan cadence |
| `maintenance_batch_size` | int > 0 | 100 | Object versions per reconciliation page |
| `object_prefix` | relative, ends with `/` | `attempts/` | Object key prefix (the MinIO policy must match) |
| `shutdown_grace_seconds` | float > 0, > heartbeat | 30 | Worker drain wait |
| `stop_grace_seconds` | float > 0 | 75 | **Validation input only**: must be ≥ `shutdown_grace + provider connect + provider read + 5 s claim release`. It configures nothing at runtime and must mirror the worker's Compose `stop_grace_period` (a unit test compares them) |

Other start-up validations: local identity needs `environment_name=local` and a loopback `bind_host`;
`https` is required for the frontend origin only in `production` (not `staging`).

## Migration job

Source: [`services/migrations/src/horizon_migrations/main.py`](../../services/migrations/src/horizon_migrations/main.py),
[`.env.example`](../../services/migrations/.env.example). No YAML policy and no AWS or API settings.

| Env variable | Required | Secret | Purpose |
|---|---|---|---|
| `MIGRATION_DATABASE_DSN` | yes (`upgrade`, `check`) | **yes** | Login acting as `app_migrator` |
| `CHECKPOINT_MIGRATION_DSN` | yes (`upgrade`) | **yes** | Login acting as `checkpoint_migrator`; plain `postgresql://` (same libpq caveat as above) |
| `CHECKPOINT_MIGRATION_ROLE` | no, default `checkpoint_migrator` | no | `current_user` the checkpoint connection must have |

## Frontend runtime configuration

`FRONTEND_GOOGLE_CLIENT_ID`, `FRONTEND_CHAT_API_URL`, `FRONTEND_INGESTION_API_URL`: see
[Frontend](../services/frontend.md#runtime-configuration).

## Compose inputs

`compose.yaml` interpolates the variables below from the root `.env` or the shell. These are Compose
inputs, not application settings; Compose maps them onto the application variables above.

| Group | Variables (local defaults are in `compose.yaml`, except required Langfuse credentials in `.env.example`) |
|---|---|
| Required | `GOOGLE_CLIENT_ID`, `MAIN_MODEL_ID`, `UTILITY_MODEL_ID` (Compose fails without them) |
| AWS | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` |
| Database credentials | `POSTGRES_PASSWORD`, `APP_MIGRATOR_PASSWORD`, `CHECKPOINT_MIGRATOR_PASSWORD`, `CHAT_DATABASE_PASSWORD`, `INGESTION_DATABASE_PASSWORD` |
| Object storage | `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `INGESTION_MINIO_ACCESS_KEY`, `INGESTION_MINIO_SECRET_KEY` |
| Langfuse | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_DATABASE_PASSWORD`, `LANGFUSE_CLICKHOUSE_PASSWORD`, `LANGFUSE_REDIS_PASSWORD`, `LANGFUSE_MINIO_USER`, `LANGFUSE_MINIO_PASSWORD`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_USER_EMAIL`, `LANGFUSE_USER_PASSWORD` |
| Host ports | `POSTGRES_PORT` (5432), `MINIO_PORT` (9000), `MINIO_CONSOLE_PORT` (9001), `OTLP_PORT` (4318), `COLLECTOR_HEALTH_PORT` (13133), `GRAFANA_PORT` (3001), `LANGFUSE_PORT` (3002), `LANGFUSE_STORAGE_PORT` (19090) |
| Project | `COMPOSE_PROJECT_NAME` (`horizon-local`; Alloy filters containers by it) |

**Not configurable by variable:** the chat API (8080), ingestion API (8081) and frontend (3000) host
ports and the frontend's API URLs and allowed CORS origin are literals in `compose.yaml`. The
component READMEs mention `CHAT_PORT`, `INGESTION_PORT` and `FRONTEND_PORT`; Compose does not read them.

Values Compose fixes for the applications (so they cannot be set from `.env`): chat and ingestion run
with `ENVIRONMENT_NAME=local`, `IDENTITY_MODE=google`, binding `0.0.0.0`, `FRONTEND_ORIGIN=http://localhost:3000`,
the Titan embedding model, `OTLP_ENDPOINT=http://collector:4318`, and database/MinIO DSNs built from the
credentials above. Chat gets `RETENTION_DAYS=30` and `MAINTENANCE_INTERVAL_SECONDS=86400` (daily
retention, overriding the YAML's 3600); ingestion gets `INGESTION_SCAN_INTERVAL_SECONDS=2`.

Changing a database or MinIO password variable does not change accounts that already exist in a volume
([Local deployment](../guides/local-deployment.md#reset)).

## Test inputs

`TEST_DATABASE_DSN`, `TEST_MINIO_ENDPOINT`, `REQUIRE_INTEGRATION`, `MIGRATION_IMAGE`,
`DOCKER_DATABASE_HOST`, `FRONTEND_TEST_PORT`: see [Testing](../guides/testing.md).
