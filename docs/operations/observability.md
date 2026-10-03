# Observability

This page inventories the metrics, traces and logs the backend services emit, where they are
routed, and how to follow one chat turn or ingestion job across them. The repository implements
instrumentation and a **local** collection stack; it contains no cloud export, no production
Collector configuration and no alert rules.

## Signal flow

| Signal | Producer | Route | Local destination |
|---|---|---|---|
| Traces | chat, ingestion API, ingestion worker (OTLP/HTTP) | Collector `traces` pipeline | Tempo, inside the `lgtm` bundle |
| Traces (GenAI subset) | same | Collector `traces/langfuse` pipeline | Langfuse |
| Metrics | same | Collector `metrics` pipeline | Prometheus, inside the `lgtm` bundle |
| Logs | JSON on stdout of `chat`, `ingestion`, `ingestion-worker` | Alloy reads container logs through the Docker socket | Loki, inside the `lgtm` bundle |
| Collector self-metrics | Collector | pushed straight to the bundle's Prometheus, bypassing the redaction pipeline | Prometheus |

The migration job, frontend, PostgreSQL and MinIO emit nothing into this pipeline. The
[system diagram](../architecture/system.md) omits the observability stack; it is defined in
[`compose.yaml`](../../compose.yaml) and [`dev/stack/`](../../dev/stack).

Local UIs (all bound to `127.0.0.1`): Grafana on `GRAFANA_PORT` (default 3001, anonymous admin),
Langfuse on `LANGFUSE_PORT` (default 3002, a seeded local account whose values are the
`LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD` values from `.env`, documented in `.env.example`), Collector health on
13133. Backend health never depends on these UIs; Langfuse is not a readiness dependency.

## How telemetry is initialised

Shared mechanics live in [`libs/observability`](../../libs/observability); each service owns its
span vocabulary, metric instruments and log allowlist.

- **Providers.** `open_providers` builds explicit tracer and meter providers (never the OpenTelemetry
  globals). The sampler is `ParentBased(ALWAYS_ON)` in both services.
- **Resource attributes.** `service.namespace=horizon`, `service.name` (`horizon-chat` or
  `horizon-ingestion`; the ingestion API and worker share one name), `service.version`,
  `service.instance.id`, `deployment.environment.name`.
- **Instance ID.** Set by `SERVICE_INSTANCE_ID` (chat) / `INGESTION_SERVICE_INSTANCE_ID`
  (ingestion), otherwise a random UUID per start. Compose sets `ingestion-api` and
  `ingestion-worker` for ingestion and nothing for chat.
- **Export.** Only when `OTLP_ENDPOINT` / `INGESTION_OTLP_ENDPOINT` is set. The value is the
  Collector's base HTTP URL; the code appends `/v1/traces` and `/v1/metrics`. Transport is
  OTLP/HTTP protobuf only, with a 2 s exporter timeout so a slow Collector cannot stall requests.
  Batching uses SDK defaults. Compose sets `http://collector:4318`.
- **Endpoint unset.** No exporter is attached, but spans still receive valid IDs. Chat therefore
  still mints and persists a root `trace_id` per attempt, and logs still carry `trace_id`.
  Metrics stay in-process.
- **Shutdown.** Flush runs off the event loop with a 5 s bound each for traces and metrics.
- **No automatic instrumentation.** There are no FastAPI, SQLAlchemy, httpx, botocore or LangChain
  instrumentors and no Langfuse SDK. Every span is created by hand, and there are no HTTP server
  spans. Chat's only HTTP telemetry is a metrics middleware; the ingestion API has none.

Settings that affect telemetry: `OTLP_ENDPOINT`, `SERVICE_INSTANCE_ID`, `LOG_LEVEL`,
`LOG_FULL_EXCEPTION_TRACE` (see [Configuration](../reference/configuration.md)).

## Metrics

Prometheus names replace `.` with `_` and add `_seconds` (unit `s`) and `_total` (counters). In
Prometheus the `job` label is `horizon/<service.name>`; the dashboards match `(horizon/)?`.

### Chat

Defined in [`observability/metrics.py`](../../services/chat/src/horizon_chat/observability/metrics.py).

| Instrument | Type, unit | Attributes | Recorded |
|---|---|---|---|
| `app.boundary.duration` | histogram, s | `app.boundary` (`agent`, `tool`, `retrieval`, `embedding`, `maintenance`, `http`), `outcome` (`ok`, `error`, `cancelled`) | Around each bounded operation. `http` is recorded by the request middleware and includes SSE delivery time; `error` when status ≥ 500. `agent` records `ok`/`error` per turn |
| `app.boundary.failures` | counter | `app.boundary` (plus `outcome` and `http.*` for `http`) | On non-cancel exceptions and HTTP ≥ 500. Failed agent turns do **not** increment it |
| `gen_ai.client.operation.duration` | histogram, s | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `outcome` | Per physical model call |
| `gen_ai.client.token.usage` | histogram, `{token}` | `gen_ai.request.model`, `gen_ai.token.type` (`input`, `output`) | When the provider reports usage |
| `gen_ai.client.operation.time_to_first_chunk` | histogram, s | `gen_ai.request.model` | First non-empty token of each physical attempt |
| `app.agent.time_to_first_chunk` | histogram, s | none | Once per attempt, from the start of turn handling (before the admission transaction) to the first answer text |
| `gen_ai.invoke_agent.inference_calls` | histogram, `{inference_call}` | none | Per turn: physical model attempts |
| `gen_ai.invoke_agent.tool_calls` | histogram, `{tool_call}` | none | Per turn: searches |
| `app.retrieval.result_count` | histogram, `{chunk}` | none | After each successful search |
| `app.retention.purged` / `app.retention.purge_failures` | counter, `{conversation}` | none | Retention pass |
| `app.recovery.failures` | counter, `{run}` | none | Every recovery iteration (every 5 s by default, including zero) |

### Ingestion

Defined in [`observability/tracing.py`](../../services/ingestion/src/horizon_ingestion/observability/tracing.py)
(`Measurements`).

| Instrument | Type, unit | Attributes | Recorded |
|---|---|---|---|
| `app.ingestion.duration` | histogram, s | `app.boundary` (`upload`, `job`, `extraction`, `embedding`, `publication`, `cleanup`, `reconciliation`, `status`, `retry`, `delete`), `outcome` | Around each bounded operation |
| `app.ingestion.failures` | counter | `app.boundary` | On non-cancel exceptions |
| `app.ingestion.jobs` | counter | `state` (`ready`, `retrying`, `failed`), `kind` (`index`, `delete`, `superseded_cleanup`) | Job outcome |
| `app.ingestion.chunks.completed` | counter | none | Each persisted chunk |
| `app.ingestion.vendor.calls` | counter | none | Each physical Titan call (SDK retries are disabled) |
| `app.ingestion.retries` | counter | `scope` (`chunk`, `job`), `category` | Chunk and job retries |
| `app.ingestion.queue.age` | histogram, s | `kind` | At claim: now minus the job's creation time |
| `gen_ai.client.token.usage` | histogram, `{token}` | `gen_ai.token.type=input`, `gen_ai.operation.name=embeddings` | Titan input tokens, from a botocore response hook |
| `app.ingestion.orphans.removed` | counter | none | Reconciliation deletes |

**What the Collector keeps.** The `transform/redact` processor keeps only these datapoint
attributes: `app.boundary`, `outcome`, `gen_ai.request.model`, `gen_ai.token.type`,
`gen_ai.operation.name`, `kind`, `state`, `error.type`, `stage`. Attributes emitted by code but
therefore **dropped before storage**: `http.request.method`, `http.response.status_class`
(chat `http` series for different methods and status classes become indistinguishable), `scope` and `category` (chunk and job retries become indistinguishable) and `gen_ai.provider.name`. The Collector only removes keys: it does not sum the colliding datapoints, so those series may be incomplete, and how Prometheus ingests the duplicates was not verified. Metric attributes never include run, user, document or job IDs (asserted by the unit tests for the paths they cover).

## Traces

Boundary spans use `boundary_span`; the turn root and model spans are created directly and marked the same way: exceptions are not recorded as events and no status
message is set; an error span carries `error.type` only. With chat content capture
enabled, model spans also carry input/output and tool spans carry arguments/results.
The Collector retains this content only on the Langfuse branch.

### Chat: one tree per attempt

| Span | Created by | Notes |
|---|---|---|
| `chat.admission` → renamed `gen_ai.invoke_agent` | Turn admission | Independent root; renamed once admission succeeds. A replayed request ends as `chat.admission`. Attributes: `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name=Horizon`, `app.conversation.id`, `app.thread.id`, `app.turn.id`, `app.run.id`, `app.user_message.id`, `app.assistant_message.id`, `app.attempt.number`, `app.agent.version`, `app.prompt.version`, `app.retrieval.version`, `app.agent.time_to_first_chunk` (seconds, set once the first answer text is sent) |
| `gen_ai.chat` | One per physical model call | `gen_ai.provider.name=aws.bedrock`, `gen_ai.request.model`, token usage when reported, `gen_ai.response.time_to_first_chunk` (seconds, set on the first non-empty token), `app.usage.available`, `app.cost.available=false`. Calls denied by the attempt budget get no span |
| `app.tool` | One per tool attempt | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, and arguments/results when content capture is enabled |
| `app.embedding` | Query embedding | `gen_ai.embeddings.dimension.count`, model, provider |
| `app.retrieval` | Vector search | `app.retrieval.version=pgvector-v1`, embedding model, `k`, chunk IDs, document-version IDs, scores, ranks |
| `app.maintenance` | Retention pass | |

`app.embedding` and `app.retrieval` are siblings under `app.tool`. A cancelled turn is marked as an
error span. Each retry is a new run with a new root trace, so every attempt has its own trace. The `trace_id` of each attempt is stored in `app.agent_runs`, returned on message history and sent in every [SSE event](../reference/events.md#chat-turn-stream-sse); the root span ID is stored too and appears on the runs in the turn response. That makes the
trace attributable even when export is disabled or the trace was not retained.

### Ingestion: upload and job are linked, not parented

- The API creates `app.upload` (and `app.status`, `app.retry`, `app.delete`). Inside `app.upload`
  it serialises a W3C `traceparent`/`tracestate` carrier into the job row (`trace_context`).
- The worker starts `app.job` as a **new root linked** to that upload span, with
  `app.job.id`, `app.document.id` and `app.message.attempt`. Children: `app.extraction`,
  `app.embedding` (one per physical Titan call, with input-token count), `app.publication`,
  `app.cleanup`. `app.reconciliation` is a separate root.
- Carriers are validated (only those two keys, at most 512 characters). Delete and cleanup jobs
  carry an empty context.
- A failed or retrying job span carries `error.type` equal to the job's error category.

### What the Collector keeps on spans

The Tempo span allowlist keeps `gen_ai.*` identity/usage attributes, both `*.time_to_first_chunk` attributes, `app.conversation.id`,
`app.thread.id`, `app.turn.id`, `app.run.id`, the message IDs, `app.attempt.number`, the three
`app.*.version` attributes, `app.document.id`, `app.job.id`, `error.type` and a few `http.*`
names; it blanks span status messages and replaces span events with a `redacted-event` that has no
attributes. Attributes emitted by the services but **dropped**: `app.retrieval.chunk_ids`,
`app.retrieval.document_version_ids`, `app.retrieval.scores`, `app.retrieval.ranks`,
`app.retrieval.k`, `app.retrieval.embedding_model`, `app.usage.available`, `app.cost.available`
and `app.message.attempt`. By configuration (not observed in a real trace), the retrieval span in Tempo and Langfuse therefore shows timing and version, not which chunks were returned. The allowlist also names attributes no service
emits (for example `app.chunk.ids`, `app.document_version.ids`, `app.boundary` on spans).
[`dev/stack/verification.md`](../../dev/stack/verification.md) and the backend-telemetry spec describe retrieval references (chunk and version IDs) as retained; that is not what the Collector does for the attributes the code actually emits.

The Langfuse branch uses `transform/langfuse_redact`: it additionally retains
`gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`. Neutral presentation
attributes are mapped to `langfuse.observation.input` / `.output` and removed.
These content fields are absent from Tempo. Both branches clear event attributes
and span status messages.

## Logs

Logs are single-line JSON on stdout, formatted by `JsonLogFormatter` (installed in ingestion's `main.py` and in chat's `build_runtime`).

- **Always present:** `timestamp` (UTC ISO-8601), `level` (lower case), `event`, `service.name`,
  `logger.name`.
- **When a span is current:** `trace_id` (32 hex), `span_id` (16 hex).
- **Errors:** `error.type` (class name). With `LOG_FULL_EXCEPTION_TRACE=true`, also
  `exception.message` and `exception.stacktrace` (the full cause chain), both redacted; the
  stacktrace keeps its last 16,000 characters and sets `app.error.stacktrace_truncated=true` when
  cut. With `false` (the default), exception text is never written: it can carry personal data.
- **Allowlisted extras only.** Chat: `conversation_id`, `turn_id`, `run_id`, `assistant_message_id`,
  `attempt_number`, `failure_category`, `persistence_pending`, `purged_count`, `failed_count`,
  `loop`, `repaired_count` (the allowlist also names `recovery_path`, which nothing emits). Ingestion: `job_id`, `document_id`, `generation`, `attempt`,
  `category`, `kind`, `deduplicated`, `loop` (it also names `version_id` and `state`, which nothing emits). Any other extra key appears only by name in
  `dropped_fields`.
- **Event names.** A service's own logger emits its message as `event`; the text is never written
  otherwise. A snake_case name missing from the service's `EVENTS` allowlist keeps its name and
  gains `event.unregistered=true`; any other own-logger text becomes `library_log`.
- **Third-party records** are `event="library_log"` (or a reviewed `psycopg.pool` event below) and
  keep their rendered `message` in both exception modes, passed through
  `horizon_observability.redaction` (credentials, tokens and URL secrets become `[REDACTED]`) and
  cut to 2,000 characters. Noisy or content-bearing namespaces are controlled by log level, not by
  dropping messages.

### PostgreSQL connection-pool warnings

The shared formatter classifies known `psycopg.pool` message templates before their arguments
are rendered. It retains the exception class as `error.type` and a valid five-character SQLSTATE
as `db.sqlstate` when the library supplies an exception argument. The rendered message is kept
like any other library message, with credentials redacted.

| Event | Meaning |
|---|---|
| `database_connection_failed` | A connection attempt failed; inspect `error.type` and `db.sqlstate` |
| `database_reconnection_failed` | The pool exhausted its reconnection budget |
| `database_broken_connection_discarded`, `database_closed_connection_discarded` | The pool discarded an unusable connection |
| `database_returned_connection_rolled_back` | A borrowed connection returned with an open or failed transaction |
| `database_connection_rollback_failed` | Rollback failed and the connection was discarded |
| `database_active_connection_closed` | A connection returned while an operation was still active |
| `database_connection_reset_failed` | The configured connection reset failed |
| `database_pool_task_failed` | A background pool task failed |

Unknown templates still use `library_log`, now with their redacted message. Records written before
this change cannot be reclassified because their original message was omitted. A connection failure reproduced against
an unavailable local port emits `database_connection_failed` with `error.type="OperationalError"`;
that reproduction does not establish the cause of a historical warning.

### Chat events

| Event | Level | Meaning |
|---|---|---|
| `turn_completed`, `turn_cancelled` | INFO | End of an attempt (`turn_failed` below) |
| `turn_failed` | ERROR | Attempt failed; carries `failure_category`, `persistence_pending` |
| `retention_completed` | INFO | Only when the batch was non-empty |
| `retention_item_failed` | WARNING | One conversation could not be purged |
| `recovery_completed` | INFO | Only when something was repaired or failed |
| `recovery_item_failed` | WARNING | One run could not be reconciled |
| `loop_dependency_unavailable`, `loop_recovered`, `loop_crashed` | WARNING / INFO / ERROR | Supervised background loop state (`loop` field) |
| `request_failed` | ERROR | A 5xx produced by an exception handler (503 dependency or identity outages, 500 integrity faults) or by the catch-all for unmapped exceptions |

### Ingestion events

| Event | Level | Meaning |
|---|---|---|
| `ingestion_upload_accepted` | INFO | `job_id`, `document_id`, `deduplicated` |
| `ingestion_job_ready`, `ingestion_cleanup_complete` | INFO | Job finished |
| `ingestion_job_retry` | WARNING | Automatic retry scheduled (`category`) |
| `ingestion_job_failed` | ERROR | Job failed terminally (a terminal category on the first attempt, or the retry budget spent) |
| `ingestion_claim_fenced` | INFO | A stale worker was fenced out |
| `ingestion_failure_record_unavailable`, `ingestion_heartbeat_unavailable`, `ingestion_claim_release_unavailable` | WARNING | Database unreachable while recording or renewing |
| `ingestion_job_corrupt` | ERROR | A queued row failed validation |
| `ingestion_listener_reconnecting` | WARNING | LISTEN connection re-established |
| `ingestion_loop_unavailable`, `ingestion_loop_recovered`, `ingestion_loop_crashed` | WARNING / INFO / ERROR | Supervised loop state |
| `request_failed` | ERROR | A 5xx produced by an exception handler (503 dependency or identity outages, 500 integrity faults) or by the catch-all for unmapped exceptions |

Two allowlisted names (`guardrail_blocked`, `ingestion_document_deleted`) have no emitter.

**Correlation caveats (expected from the code; not checked at runtime).**
`turn_completed` is written outside the turn's active span, so it probably has no `trace_id`.
`turn_failed` and `turn_cancelled` are written inside it. `request_failed` is logged by exception
handlers outside any span. Uvicorn is started with its default logging configuration, so its own
access and error lines are plain text, not JSON; Alloy gives them only the `compose_service` and `container_id` labels, not `service_name` or `level`, so `{service_name=…}` queries and the dashboard log panels probably will not show them.

### Log transport (local)

Alloy discovers only containers in the Compose project labelled `horizon.logs=true` (`chat`,
`ingestion`, `ingestion-worker`), parses the JSON, and pushes to Loki with labels `service_name`,
`level`, `compose_service`, `container_id` and `trace_id` / `span_id` as structured metadata.
Grafana's Loki datasource has a derived field that turns a `trace_id` in a log line into a Tempo
link, and the Tempo datasource links back to Loki.

## Collector pipeline

Configuration: [`dev/stack/collector/config.yaml`](../../dev/stack/collector/config.yaml)
(`otel/opentelemetry-collector-contrib` 0.159.0).

| Pipeline | Processors, in order | Exporter |
|---|---|---|
| `traces` | `memory_limiter` (256 MiB), `transform/redact`, `batch` | `otlphttp/lgtm` → `http://lgtm:4318` (queue 1000, retry up to 60 s) |
| `traces/langfuse` | `memory_limiter`, `transform/langfuse_redact`, `filter/genai`, `transform/langfuse`, `batch` | `otlphttp/langfuse` → `http://langfuse-web:3000/api/public/otel`, Basic auth from the Langfuse project keys (queue 256, retry up to 60 s, 5 s timeout) |
| `metrics` | `memory_limiter`, `transform/redact`, `batch` | `otlphttp/lgtm` |

- The OTLP receiver listens on 4317 (gRPC) and 4318 (HTTP). Compose publishes only 4318 and the
  health endpoint 13133, both on `127.0.0.1`. The applications use HTTP.
- `filter/genai` keeps only spans named `gen_ai.*`, `chat.admission`, `app.tool`, `app.retrieval`,
  `app.embedding`, `app.job` (it also lists `app.agent` and `app.model`, which no service creates).
  Langfuse therefore does not receive `app.upload`, `app.status`, `app.retry`, `app.delete`,
  `app.extraction`, `app.publication`, `app.cleanup`, `app.reconciliation` or `app.maintenance`.
- `transform/langfuse` sets `langfuse.session.id` from `app.conversation.id` and the observation
  type (`generation` for `chat`, `embedding` for `embeddings`, `tool` for `execute_tool`).
  The exporter uses native ingestion version 4 with the same project-key Basic auth.
- Langfuse export uses a bounded in-memory queue: data can be lost after retries are exhausted or
  if the Collector restarts. Retried chat attempts keep distinct trace IDs and attempt numbers.
- There is no logs pipeline; logs bypass the Collector through Alloy.
- Chat `capture_ai_content` is enabled by the local service overlay and disabled
  in the base configuration; `CAPTURE_AI_CONTENT` overrides it. Completed calls
  capture input/output once, with system instructions separated in the canonical
  Bedrock representation. Failed streams capture bounded partial text with a
  partial/truncated marker. Tool spans capture arguments/results. Langfuse keeps
  this content; Tempo removes it regardless of the capture setting.
- `LOG_FULL_EXCEPTION_TRACE` independently controls exception logging.

## Grafana dashboards and datasources

Provisioned from [`dev/stack/grafana/`](../../dev/stack/grafana) (the mount replaces the bundle's
own provisioning). Datasources: Prometheus (default), Tempo, Loki. Folder `Horizon`:

- **Horizon Chat** (`chat.json`): request errors (HTTP ≥ 500 only), p95 latency of agent, model,
  first chunk, model first chunk and retrieval, tool-call rate, input/output token rate, and a
  correlated Loki log panel.
- **Horizon Ingestion** (`ingestion.json`): completed jobs by kind, job errors, queue age p95, job
  latency p95, completed chunks, vendor calls, retries, orphans removed, and a log panel.

Not covered by any panel: `app.retrieval.result_count`, inference-call counts, retention and
recovery counters, `app.ingestion.failures`, ingestion API request metrics, Collector self-metrics,
and any trace view. Because chat turns stream with HTTP 200, a failed turn does **not** appear in
"Request errors"; use `increase(app_boundary_duration_seconds_count{app_boundary="agent",outcome="error"}[1h])` (it also counts cancelled turns) or search logs for `turn_failed`.

## Correlating one request

**A chat turn.**

1. Read `trace_id` from any [SSE event](../reference/events.md#chat-turn-stream-sse) envelope, or
   from the assistant message in `GET /v1/conversations/{id}/messages` (also present on each run in
   `GET …/turns/{turn_id}`).
2. Grafana → Explore → Tempo: search by that trace ID. Langfuse shows the same ID with the
   conversation as the session (GenAI spans only).
3. Logs: in Loki run `{service_name="horizon-chat"} | json | trace_id="<trace-id>"`.
4. To tie feedback to a specific attempt, run
   [`dev/stack/postgres/history-join.sql`](../../dev/stack/postgres/history-join.sql) against the database (usage in [`dev/stack/README.md`](../../dev/stack/README.md#verify-and-inspect)).

A missing trace does not mean the turn did not happen: it may have been unsampled, not exported,
dropped after Langfuse retries, expired from retention, or lost with a volume reset. History and
feedback stay attributable through `app.agent_runs`. Do not replay a model call to recreate a
trace.

**An ingestion job.** Take `job_id` from the upload acceptance or status response, then in Loki run
`{service_name="horizon-ingestion"} | json | job_id="<job-id>"`. The `app.job` trace in Tempo is a
separate root linked to the `app.upload` trace.

## Retention of telemetry (local)

Prometheus keeps seven days (`--storage.tsdb.retention.time=7d`). Tempo and Loki follow the pinned
`grafana/otel-lgtm` bundle defaults, which this repository does not override. Langfuse retention is
managed by Langfuse. All telemetry lives in Docker volumes (`lgtm-data` and the `langfuse-*`
volumes) and is lost by `docker compose down --volumes`.

## Verifying the pipeline

[`dev/stack/smoke.py`](../../dev/stack/smoke.py) sends synthetic OTLP/HTTP JSON canaries without AWS
or a Google token. Run it from the repository root with the stack up:

```zsh
uv run --locked python dev/stack/smoke.py
uv run --locked python dev/stack/smoke.py --service horizon-ingestion
```

`--otlp` overrides the Collector URL (default `http://localhost:4318`). Each run sends two
attempts with fresh trace IDs and prints one JSON line per attempt (`event: local_stack_canary`,
`trace_id`, `conversation_id`). The script asserts only that the Collector accepted the data
without partial rejection. Inspect Tempo, Langfuse, Loki and Prometheus manually: the operational
sibling span (`app.persistence` or `app.publication`) must be absent from Langfuse, and the
canary's input/output must appear only in Langfuse. Its `authorization` and status
message canaries must be absent from both destinations. To exercise Alloy as well, run it inside an opted-in container,
as shown in [`dev/stack/README.md`](../../dev/stack/README.md#verify-and-inspect).

Limits of this check: its span names and attribute names do not all match what the services emit
(it uses `app.agent`, `app.persistence` and `app.chunk.ids`, which real code does not), so a
passing canary proves routing and redaction, not that real retrieval attributes survive. A
dated acceptance record from the stack's introduction exists in
[`dev/stack/verification.md`](../../dev/stack/verification.md); this manual did not re-run it.

## Known gaps

- **No alerting.** No alert rules, Alertmanager or Grafana alerting provisioning exist in the
  repository, and no thresholds or on-call routing are defined.
- **No cloud or production telemetry path.** Only the local Collector, Grafana bundle and Langfuse
  are configured. `config/staging.yaml` and `config/production.yaml` hold no observability settings.
- **Redaction allowlist and emitted attributes differ** (see above), which removes retrieval
  detail and the usage/cost availability markers from stored traces.
- **Telemetry configuration is not validated in CI**; the workflows do not reference the Collector,
  dashboards or `smoke.py`.

## Implementation references

[`libs/observability`](../../libs/observability),
[`services/chat/src/horizon_chat/observability/`](../../services/chat/src/horizon_chat/observability),
[`services/ingestion/src/horizon_ingestion/observability/`](../../services/ingestion/src/horizon_ingestion/observability),
[`dev/stack/alloy/config.alloy`](../../dev/stack/alloy/config.alloy),
[`dev/stack/grafana/provisioning/`](../../dev/stack/grafana/provisioning).
