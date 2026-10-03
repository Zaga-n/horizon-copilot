## 1. Local database and backend lifecycle

- [x] 1.1 Add pinned Compose services, persistent pgvector PostgreSQL and MinIO, health checks, and environment templates; verify a fresh stack reaches database/object-store health without a frontend.
- [x] 1.2 Provision separate application migrator, checkpoint migrator, chat runtime, and ingestion runtime roles; verify each role's allowed and denied schema operations with SQL checks.
- [x] 1.3 Wire the dedicated migration service/container (shared `alembic upgrade head` followed by checkpoint setup) as a successful-completion dependency of chat and ingestion startup; verify fresh start, repeat start, and deliberate setup failure behavior.
- [x] 1.4 Configure a private versioned MinIO bucket for API-mediated uploads, the always-on ingestion worker with PostgreSQL LISTEN/NOTIFY and periodic recovery, bounded leases/retries, orphan/deletion cleanup, and worker health; verify accepted jobs survive missed wakeups and listener restart without any S3 event integration.
- [x] 1.5 Add local runbook for AWS/MinIO credentials, Google ID-token bearer/client-ID setup, local identity mode, empty-index behavior, worker recovery, 30-day daily chat retention and catch-up, startup, restart, backup, and explicit volume reset; verify instructions on a fresh local volume.

## 2. Grafana LGTM collection

- [x] 2.1 Configure the Collector for chat/ingestion OTLP trace and metric reception, Tempo export, and Prometheus OTLP/HTTP ingestion; verify a synthetic turn and ingestion job trace and metric reach Grafana.
- [x] 2.2 Configure Alloy container-log collection into Loki with service and trace correlation; verify chat and ingestion logs link to their Tempo traces.
- [x] 2.3 Provision Grafana Loki/Tempo/Prometheus datasources and concise chat/ingestion dashboards; verify request errors, model/agent latency, first chunk, retrieval, tool calls, tokens, ingestion jobs, and vendor retries render from smoke-test data.

## 3. Langfuse projection

- [x] 3.1 Add pinned default local Langfuse services with required backing services, persistent volumes, and secret configuration; verify its UI and OTLP ingestion endpoint health.
- [x] 3.2 Add Collector common redaction, complete Tempo branch, and ancestor-closed GenAI Langfuse branch with OTLP/HTTP auth and bounded retry; verify the same trace ID, connected parent tree, and no duplicate model generation.
- [x] 3.3 Verify Langfuse outage leaves backend readiness and Grafana telemetry working; inspect queued/retried export and confirm secrets/content stay absent by default.

## 4. End-to-end verification

- [x] 4.1 Run fresh-volume and restart smoke tests for direct upload with progress, lost-response replay, worker/listener restart, selective chunk retry, deletion, retention catch-up, cited in-scope turn, and empty-index turn; verify persisted history and ordered migration/setup lifecycle.
- [x] 4.2 Inspect Grafana and Langfuse for one turn and capture a short verification report with matching trace IDs, log links, metrics, redaction evidence, and resource requirements.

## 5. Evaluation correlation verification

- [x] 5.1 Preserve execution IDs and bounded evidence/configuration references in Collector projections and map conversation session grouping for the pinned Langfuse version; verify two attempts share a conversation but retain different trace/run IDs in both destinations.
- [x] 5.2 Add a runbook join example from user message through run/answer to explicit feedback and traces; verify with a failed attempt followed by a rated retry and document missing/export-failed/expired trace handling without copying feedback to telemetry.

## Acceptance evidence — 3 October 2026

All remaining tasks were verified in an isolated fresh-volume deployment with real
Bedrock and explicit loopback local identity. See `dev/stack/verification.md` and
`dev/stack/acceptance-2026-10-03.json` for checks, fault-injection boundaries, trace
IDs, feedback joins and measured resources. Google OAuth login was not exercised.
