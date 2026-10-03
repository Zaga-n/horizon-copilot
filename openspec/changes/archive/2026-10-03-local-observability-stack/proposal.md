## Why

The backend needs a reproducible local PostgreSQL and observability environment where chat behavior, failures, model calls, and retrieval can be inspected. This environment must have a clear boundary from the backend's telemetry instrumentation and from the later frontend and ingestion services.

## What Changes

- Add a local Compose deployment for PostgreSQL with pgvector, chat and ingestion FastAPI services, an always-on database-listening ingestion worker, MinIO, a shared one-shot migration service/container that applies application migrations followed by LangGraph checkpoint setup.
- Add the bundled Grafana LGTM development image with Loki, Grafana, Tempo, Prometheus, plus Alloy collection, and dashboards for logs, metrics, and traces.
- Add a local Langfuse deployment and Collector routing of one backend trace to Grafana Tempo and a rooted GenAI view in Langfuse.
- Document configuration, secrets, health checks, startup order, and a smoke test covering both observability destinations.

## Capabilities

### New Capabilities

- `local-development-stack`: Reproducible chat and ingestion startup, direct API uploads, durable database jobs and worker readiness, daily chat maintenance, database readiness, migration ordering, and configuration.
- `local-telemetry-stack`: Collection and verification of correlated Grafana logs, metrics, traces, and Langfuse GenAI traces.

### Modified Capabilities

None.

## Impact

Creates Compose and deployment configuration, local secret examples, dashboards, and a runbook. Consumes the chat contract in `horizon-agent-backend` and ingestion contract in `document-ingestion-pipeline`; it does not implement either service's behavior or the frontend.

Trace projection also preserves durable execution/history join attributes and conversation session grouping for future evaluation. No automatic evaluator or feedback-copy pipeline is introduced.
