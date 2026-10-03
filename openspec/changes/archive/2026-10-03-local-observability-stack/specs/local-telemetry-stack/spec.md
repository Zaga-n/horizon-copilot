## Purpose

Make chat and ingestion logs, traces, and metrics visible in Grafana while providing a connected chat GenAI trace view in local Langfuse.

## ADDED Requirements

### Requirement: Grafana LGTM visibility
The local stack SHALL collect chat and ingestion structured logs, traces, and metrics through Alloy or the OpenTelemetry Collector, store metrics in Prometheus, and make all three signals queryable in Grafana with linked trace and log identifiers. It SHALL provide useful dashboard views for request health, agent and model latency, time to first chunk, retrieval, tool calls, token usage, ingestion jobs, and vendor retries.

#### Scenario: Inspect one chat turn
- **WHEN** a developer completes a streamed chat turn
- **THEN** Grafana shows its trace and correlated logs plus service and GenAI metric observations

### Requirement: Single-trace Langfuse view
The stack SHALL send the same trace ID to Grafana Tempo and Langfuse. The Langfuse view SHALL retain the chat root and every parent of a retained agent, model, tool, embedding, or retrieval span, while excluding unrelated operational spans and unapproved content.

#### Scenario: Compare the two views
- **WHEN** a developer opens a turn in Grafana and Langfuse
- **THEN** both views have the same trace ID and the Langfuse view has a connected GenAI span tree

### Requirement: Separation and failure handling
The stack SHALL keep backend emission independent of either observability destination and SHALL not require Langfuse availability for chat readiness. It SHALL avoid duplicate GenAI generations and SHALL redact credentials and protected content on both export paths.

#### Scenario: Langfuse unavailable
- **WHEN** Langfuse is down while the backend and Grafana stack are healthy
- **THEN** chat remains available and Grafana telemetry remains queryable

### Requirement: Preserve execution joins across destinations
Collector projection SHALL preserve the backend conversation/thread, turn, run, user-message, assistant-message, and attempt attributes on the retained root and keep the same trace ID across Tempo and Langfuse. Langfuse session grouping SHALL use conversation_id, while each retry SHALL remain a distinct execution trace. Projection SHALL retain bounded retrieved chunk/version references and configuration identities without exporting source text by default. The local runbook SHALL show how to join stored history/feedback to those traces and explain independent retention and missing-trace behavior; it SHALL not require a second app tracing or feedback-export pipeline.

#### Scenario: Inspect rated retry
- **WHEN** a developer inspects feedback on a completed retry after an earlier failed answer
- **THEN** its stored run/trace keys select the retry in both destinations and both executions share the same conversation session

#### Scenario: Expired trace
- **WHEN** history and feedback exist after destination trace retention has expired
- **THEN** the runbook identifies the missing trace without treating the stored rating as unlinked or recreating execution
