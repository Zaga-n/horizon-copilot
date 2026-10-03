## Why

The project needs a durable, async Horizon Europe assistant backend before a frontend or ingestion pipeline is built. Its chat history, retrieval behavior, safety boundaries, and telemetry contract need to be explicit so those later components can integrate without owning agent state.

## What Changes

- Add an async chat API with conversation ownership, cited message history, per-answer like/dislike feedback, thread feedback, and streamed turn events with a retryable failed state.
- Authenticate protected requests with a Google ID token bearer from a future Google sign-in frontend, with no custom developer API key.
- Run daily recoverable cleanup of conversations inactive for a configurable period (30 days by default), including their messages, feedback, and LangGraph state; uploaded documents remain until explicit deletion.
- Build a LangChain `create_agent` assistant with one pgvector RAG tool, bounded retries and calls, conversation summarization, and model-based input guardrails.
- Persist application records in PostgreSQL `app` and LangGraph checkpoints in `langgraph`, with one app-schema migration history shared with the later ingestion service, owned by a dedicated one-shot migration container that also runs deployment-time checkpoint setup.
- Emit structured logs, OpenTelemetry traces, and bounded service and GenAI metrics for a separate local observability deployment to collect.
- Define a read contract for Horizon document chunks and basic source metadata used in citations, with agent-driven metadata filtering deferred, written by the separate `document-ingestion-pipeline` change; frontend implementation remains later work.

## Capabilities

### New Capabilities

- `chat-conversations`: Verified bearer identity, owned conversation and message persistence, citation and feedback records, history API, async streaming turn contract, and inactivity retention.
- `horizon-agent`: Scoped agent behavior, RAG retrieval, guardrails, bounded execution, and graceful answers.
- `backend-telemetry`: Backend log, trace, and metric emission contract.

### Modified Capabilities

None.

## Impact

Creates the chat service, API, shared application-schema model package, dedicated migration service/container with Alembic history and LangGraph setup, agent and retrieval modules, and tests. Requires PostgreSQL with pgvector, Bedrock model access, and an OTLP collector endpoint. The ingestion service is specified in `document-ingestion-pipeline`; local deployment and Langfuse are specified in `local-observability-stack`.

Execution attribution adds shared-schema turn/run records and owner-readable run/trace identifiers, optional dislike comments, and conversation ratings with a context boundary. The backend preserves these joins independently of telemetry availability to support a future evaluation loop; automated scoring, implicit-feedback analytics, and dataset export remain deferred.
