## Context

The chat and ingestion proposals define one assistant database, two schemas, OTLP trace/metric emitters, JSON stdout logs, and a single GenAI trace owner per service. This change packages local services around those contracts; see its capability specs for observable outcomes.

## Goals / Non-Goals

**Goals:** reproducible chat and ingestion startup with MinIO; inspectable Grafana logs, Prometheus metrics, and Tempo traces; one connected Langfuse view of the same GenAI trace.

**Non-Goals:** production HA sizing, public exposure of local UIs, a frontend, and application-level chat or ingestion code.

## Decisions

### Compose lifecycle

The root `compose.yaml` is the local entrypoint. All local-only provisioning,
Collector/LGTM configuration, Grafana dashboards, default Langfuse services,
smoke checks, and runbooks live in `dev/stack/`. Shared schema ownership/grant SQL
remains in `libs/schema/sql`. Compose constructs migration and runtime DSNs from
local password inputs; the root `.env.example` does not duplicate those DSNs.
The former standalone migration Compose file and `dev/minio` location are removed.
Container APIs use Google identity; host processes can use validated loopback
local identity as documented in the runbook.


Use root Compose as the single entrypoint: plain `docker compose up` starts the backends, bundled LGTM, and Langfuse with its backing services. Run PostgreSQL with pgvector and persistent storage, MinIO with a private versioned document bucket, the dedicated one-shot `services/migrations` container (shared `app` Alembic history followed by serialized `langgraph` checkpoint setup), then chat, ingestion API, and a separate always-on ingestion worker. Both APIs and the worker depend on successful completion of the same migration job using `service_completed_successfully`; a nonzero exit prevents their startup. Chat readiness verifies both schemas; ingestion readiness verifies `app` and MinIO. Compose `depends_on` alone is not the runtime guarantee. Uploads go through the ingestion API, which stores originals in MinIO and commits metadata/jobs in PostgreSQL before acknowledging. Provision no MinIO event webhook or presigned-upload flow. The worker maintains a dedicated PostgreSQL LISTEN connection with reconnect/backoff, startup scans, and configurable periodic scans (default 2 seconds); NOTIFY is a wakeup hint, not the durable queue. Configure job/permit leases, bounded retries, orphan-object cleanup, and deletion cleanup. Worker health exposes listener/recovery-scan liveness separately from API readiness; loss of a wakeup does not lose accepted work. Configure daily chat maintenance with 30-day inactivity retention, persistent run/lease coordination, and startup catch-up; documents do not expire with conversations. Document the Google OAuth client ID and verified ID-token bearer contract for both APIs, without a developer API key or frontend client secret. Local development identity is explicit and published API ports bind to host loopback. Keep real AWS credentials out of Compose files and images; use ignored secret files or mounted AWS profiles. Local database, MinIO, and Langfuse credentials have explicit Compose development defaults with optional overrides. Empty-index startup remains valid.

### Telemetry flow and components

Chat, ingestion API, and ingestion worker send OTLP traces and metrics to one local OpenTelemetry Collector and write JSON logs to stdout. Alloy reads container logs and sends them to Loki, adding service/resource labels and parsing trace IDs; neither app exposes a duplicate Prometheus scrape endpoint. The Collector exports operational traces to Tempo, metrics via OTLP/HTTP to Prometheus in the bundled `grafana/otel-lgtm` image, and filtered GenAI trace projections to Langfuse over OTLP/HTTP. Grafana has provisioned Tempo, Loki, and Prometheus datasources, cross-links trace IDs, and dashboards for chat health/GenAI latency and ingestion jobs/retries. Pin the LGTM components together after a local smoke test; avoid collecting the same signal twice.

Langfuse runs locally with its documented required backing services and persistent volumes. Keep its database/queue services separate from the assistant's two-schema PostgreSQL database; the assistant database remains exactly the `app` and `langgraph` design. Langfuse is optional for service readiness and its exporter uses a bounded queue and retry. One trace per operation goes through the Collector; there is no second Langfuse SDK callback in either service. The Langfuse branch keeps a root and all ancestors of agent, model, tool, retrieval, and embedding spans, preserves IDs and parent IDs, strips unrelated operational subtrees, and adds only destination-specific attributes. Tempo receives complete operational traces with content removed; a common redaction stage runs before the split. Content capture remains disabled by default, with a documented local opt-in. Langfuse uses OTLP/HTTP with its required auth/header configuration.

### Credentials, access, and verification

Provision distinct app migration, checkpoint migration, chat runtime, and ingestion runtime database roles. Run migrations/setup with one-shot privileged credentials and each service with its runtime credentials. Do not publish PostgreSQL, MinIO, Collector, or Langfuse backing service ports beyond loopback unless a developer explicitly changes the local profile. Grafana allows anonymous local access; Langfuse seeds a development account with Compose defaults. All published ports remain loopback-bound. Validate the stack with one uploaded sample document, a completed ingestion job with chunk progress, an idempotent upload replay, a listener restart, selective retry, deletion, retention catch-up, a cited in-scope chat turn, and an empty-index turn: history persists, a Grafana trace and correlated log resolve, Prometheus has observations, and Langfuse resolves the same trace ID with a connected parent chain. Verify that secrets, prompt text, and document excerpts are absent from default exports.

## Risks / Trade-offs

- **LGTM plus self-hosted Langfuse is resource intensive** → document resource requirements; start Langfuse by default, while runtime readiness remains independent of Langfuse availability.
- **Double collection or duplicate generations** → assign one receiver/exporter per signal and use the backend's single OTel callback path.
- **A filtered Langfuse span can lose its parent** → mark the root and all retained ancestors and check parent closure in the smoke test.
- **Exporter credentials or Langfuse availability fail** → queue/retry within a cap, report collector health, and keep backend readiness independent.
- **Local credentials leak into Git or logs** → real credentials remain in ignored secret files; only explicit development defaults are committed, and both Collector branches redact before export.

## Migration Plan

Add pinned Compose/configuration, run a fresh-volume startup through the ordered jobs, then run the telemetry smoke test. Existing local volumes are not modified implicitly; document backup and explicit reset commands. Rolling back the deployment config restores the previous local stack while database schema rollback remains governed by the backend change.

### Evaluation join projection

Preserve conversation_id/thread_id, turn_id, run_id, user_message_id, assistant_message_id, and attempt_number as metadata through both branches. Map conversation_id to the pinned Langfuse ingestion contract's session grouping and preserve the original run/trace identity; verify that mapping against the deployed version before release. Keep bounded chunk/version references and prompt/agent/retrieval configuration versions. Session grouping combines turns without merging their traces. Do not add high-cardinality metric labels or a second Langfuse callback. Explicit feedback remains in app tables and is joined by stored keys for now; a later exporter/evaluator is separate scope. Document that chat records default to 30-day inactivity retention and destination trace retention can differ; correlation IDs do not guarantee trace availability. Content capture may include approved/redacted prompts and tool input/output, but does not promise private reasoning or an unrestricted checkpoint dump.
