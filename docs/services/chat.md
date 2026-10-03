# Chat service

`services/chat` is the conversational backend. It owns conversations, turns, messages, feedback and
the Horizon agent: a bounded LangGraph/LangChain agent that answers questions from the documents
indexed by the [ingestion service](ingestion.md), streams the answer over Server-Sent Events (SSE)
and records every attempt durably so a client can recover from disconnects.

**Responsibilities:** verify the caller's identity; admit, stream, persist, retry and reconcile
turns; run the agent against Amazon Bedrock; retrieve evidence from pgvector; delete expired
conversations.
**Not responsible for:** indexing or deleting documents (read-only access to document tables),
schema changes (it never runs DDL), serving the UI.

Package: [`services/chat/src/horizon_chat`](../../services/chat/src/horizon_chat). Image:
[`services/chat/Dockerfile`](../../services/chat/Dockerfile).

## Entrypoint and lifecycle

`python -m horizon_chat.main` loads settings and calls `uvicorn.run(create_app(...))` as **one
process**. Start-up happens in the FastAPI lifespan
([`bootstrap/app.py`](../../services/chat/src/horizon_chat/bootstrap/app.py),
[`bootstrap/runtime.py`](../../services/chat/src/horizon_chat/bootstrap/runtime.py)):

1. Load secrets (`DATABASE_DSN`, `CHECKPOINT_DATABASE_DSN`, optional AWS keys).
2. Configure JSON logging and telemetry (OTLP is optional).
3. Open the SQLAlchemy engine (lazy) and the LangGraph checkpoint connection pool. **If PostgreSQL
   is unreachable at start, the pool open fails and the process exits.**
4. Build the Bedrock client, the three chat models, the query embedder, the retriever and the agent.
   No Bedrock inference call is made while building (a model ID containing `application-inference-profile` makes the library call `GetInferenceProfile` at construction).
5. Build the run ledger, conversation store, recovery buffer and readiness probe.
6. Start two supervised background loops, then serve.

Start-up **never runs DDL**. A schema that does not match the code only makes `/ready` return 503
and makes the retention pass skip; requests that hit mismatched tables fail (typically 500, or 503 for a missing grant).

| Background loop | Cadence | Purpose |
|---|---|---|
| `failure-reconciliation` | `recovery_interval_seconds` (5 s) | Persist failures that could not be written when they happened |
| `conversation-retention` | `maintenance_interval_seconds` (3600 s in YAML; Compose sets 86400) | Delete inactive conversations |

During a database outage a loop backs off exponentially, capped at `maintenance_interval_seconds`,
which means the 5 s reconciliation loop can wait up to an hour (YAML) or a day (Compose) to retry after a long outage; reconciliation also runs when a client touches the turn (see below). A loop that raises any
other exception is logged as `loop_crashed`, stays stopped, and makes `/health` return 503 while the
process keeps serving.

**Shutdown.** The loops get `maintenance_shutdown_grace_seconds` (5 s) to finish, then are
cancelled; resources close in reverse order. Compose gives the container `stop_grace_period: 15s`,
and a settings test requires the shutdown grace to be at most half of it. There is no final drain of
the failure buffer and no explicit lease release at shutdown: a turn killed with the process is
recovered by lease expiry. How uvicorn treats in-flight SSE connections on SIGTERM is not
determined from the repository (no graceful-shutdown timeout is configured).

**Health.** `/health` is process liveness (fails only when a loop crashed). `/ready` is liveness
plus database checks; neither calls AWS or Google. Details: [API reference](../reference/api.md#health-and-readiness).

## Code layout and layer rules

The service follows a hexagonal layout enforced by import-linter contracts in the root
[`pyproject.toml`](../../pyproject.toml) (run by `scripts/quality.sh`).

| Package | Responsibility | May not import |
|---|---|---|
| `api/` | FastAPI routers, bearer dependency, SSE framing, problem+json handlers, request metrics | adapters, bootstrap, config, db, genai |
| `application/` | One action per use case: `submit_turn`, `retry_turn`, `recover_failures`, conversations | FastAPI, SQLAlchemy, LangChain, boto3, OpenTelemetry, `horizon_schema`, and the implementation packages (`adapters`, `api`, `bootstrap`, `config`, `db`, `genai`, `workers`); it may use `observability` |
| `domain/` | Pure values and rules: run/turn statuses, failure categories, lease predicates, stream events, sources | everything above, plus `application` and `observability` |
| `ports/` | Protocols (`RunLedger`, `ConversationStore`, `FeedbackStore`, `CheckpointStore`, `IdentityVerifier`, `HorizonAgent`) and error base classes | as `domain/` |
| `adapters/` | Google and local identity verifiers | `bootstrap` |
| `db/` | All SQL; the only package that may import `horizon_schema` | `bootstrap` |
| `genai/` | LangChain agent, middleware, retrieval | `bootstrap` |
| `observability/` | Spans, metrics, log allowlists | no contract; kept free of `bootstrap` by convention |
| `workers/` | Loop bodies | adapters, config, db, genai, `bootstrap` |
| `config/` | Settings and secrets | |
| `bootstrap/` | The only composition root | |

Shared libraries may be imported only from designated layers (for example `horizon_genai` only from
`genai/` and `bootstrap/`); see [Shared libraries](../architecture/shared-libraries.md).

## One turn, step by step

Entry: `POST /v1/conversations/{id}/turns:stream` →
[`api/routers/turns.py`](../../services/chat/src/horizon_chat/api/routers/turns.py).

1. **Authenticate** the bearer token ([Authentication](../guides/authentication.md)).
2. **Admit** ([`application/submit_turn.py`](../../services/chat/src/horizon_chat/application/submit_turn.py), [`db/runs.py`](../../services/chat/src/horizon_chat/db/runs.py) `SqlRunLedger.admit`): first persist buffered failure intents for the conversation (separate transactions). Then, in one transaction that locks the conversation row: resolve the `Idempotency-Key` (replay returns the stored turn as JSON; different content is 409); reject with 409 if another attempt holds the lease;
   otherwise insert the turn, the user message, a pending assistant message and the run, and lease
   the conversation for `turn_lease_seconds` (150 s). The admission commit happens before any bytes
   are streamed, so these outcomes are ordinary HTTP statuses.
3. **Stream** ([`application/_turn_stream.py`](../../services/chat/src/horizon_chat/application/_turn_stream.py)):
   emit `started`; pin the LangGraph checkpoint this attempt resumes from (the thread's current
   checkpoint, or for a retry the failed attempt's base); run the agent; for every answer fragment,
   **save the visible prefix, then emit the `delta`**; on success commit the completed message and
   sources, then emit `sources` and `completed`.
4. **Fail or cancel.** Any error is mapped to a failure category, written with bounded retries
   (3 attempts, 0.5 s doubling to 4 s, whole cleanup bounded to about 34 s), then `failed` is
   emitted. A client disconnect cancels the stream and records `cancelled`, keeping the partial text.

Event contract: [Events reference](../reference/events.md#chat-turn-stream-sse).

## The Horizon agent

Built in [`genai/horizon_agent/agent.py`](../../services/chat/src/horizon_chat/genai/horizon_agent/agent.py)
and run by [`runner.py`](../../services/chat/src/horizon_chat/genai/horizon_agent/runner.py) under
a turn deadline (`turn_deadline_seconds`, 120 s, covering the graph only).

| Model role | Model setting | Streaming | Reasoning effort | Used for |
|---|---|---|---|---|
| decision | `MAIN_MODEL_ID` | no | `main_reasoning_effort` (low) | Decide whether to search; bound to `rag_search` |
| final | `MAIN_MODEL_ID` | yes, tagged `horizon-final` | `main_reasoning_effort` (low) | Tool-free final answer; the only source of streamed text |
| utility | `UTILITY_MODEL_ID` | no | `utility_reasoning_effort` (none) | Scope guardrail, conversation summary, query rewrite |

All roles are Bedrock Converse chat models built by
[`libs/genai`](../../libs/genai/src/horizon_genai/bedrock.py) with `max_tokens` = `max_output_tokens`
(4096), SDK retries disabled (the service owns retries) and the configured connect/read timeouts.

**Middleware, outermost first:** input guardrail → bounded summarization → model-call limit →
tool-call limit → final-answer phase → model retry → protocol translation → tool retry → tool
telemetry.

- **Guardrail.** The utility model classifies the latest message (plus up to the last four prior
  messages, treated as untrusted data and cut to 2000 characters) as `in_scope`, `ambiguous`,
  `out_of_scope` or `injection`. Anything other than `in_scope` ends the run with a fixed reply from
  [`prompts.py`](../../services/chat/src/horizon_chat/genai/horizon_agent/prompts.py), emitted as a
  single `delta`; no search or final model call happens. The guardrail also records whether the
  question requires document evidence.
- **Summarization.** When the history exceeds `summary_trigger_tokens` (12000) the utility model
  summarises it, keeping the latest `summary_keep_messages` (8).
- **Search tool.** `rag_search(query, k)`: `query` 1–2000 characters, `k` 1–8 (default 5). The
  caller's identity comes from the runtime context, never from the model. At most `max_tool_calls`
  (2) searches run; at the limit the tool returns an empty result flagged `limit_reached`.
- **Final phase.** If the question requires evidence and the model did not search (or its draft reused citation markers), the service injects a search itself. If evidence was required and none was found, a fixed "no evidence" reply
  is emitted without calling the final model. Otherwise the final model runs with no tools and an
  allowed-citation-marker rule. A tool call in the final output is an error.
- **Budgets.** Two independent counters: *model calls* (`max_model_calls`, 5; one graph pass may
  contain a decision and a final call) and *physical attempts* (`max_physical_model_attempts`, 24;
  every Bedrock request including guardrail, summary, rewrite, retries and the query embedding). The
  last physical slot is reserved for the final answer. Exceeding either ends the attempt as
  `budget_exhausted`.
- **Retries.** Transient Bedrock errors (throttling, service unavailable, model not ready/timeout,
  internal error, connection and read timeouts) are retried up to `retry_attempts - 1` more times
  with backoff `0.5 s → 4 s` (jittered for model and tool retries, not for utility-model retries), but never once visible text has started streaming. A search is retried only when it fails as unavailable (query-embedding provider or index database); rejected, invalid-output and integrity failures are not retried. The query-rewrite call is not retried: on a Bedrock client error, SDK error or timeout the original (bounded) query is used; other errors end the attempt.
- **Citations.** Evidence excerpts get per-attempt markers `S1`, `S2`, …. After streaming, the runner checks every bracketed token matching `[S…]` or `[<digits>]` against this attempt's issued markers (`S1`, `S2`, …) — numeric `[n]` brackets are never valid, and neither is bracketed text beginning with a capital S such as `[Smith 2020]` — and that a search-requiring answer cites something; otherwise the attempt fails as
  `invalid_citations` — and because validation follows streaming, the already-visible text stays
  saved on the failed message. Sources are returned in order of first citation.

## Retrieval

[`db/retrieval.py`](../../services/chat/src/horizon_chat/db/retrieval.py) runs one parameterised
query over `app.document_chunks` joined to versions, documents and users:

- The query is embedded with the Titan text embedding model (1024 dimensions, normalised) after an
  optional utility-model rewrite; vectors are rejected unless 1024 finite numbers.
- Ranking is cosine distance (`<=>`), ties broken by chunk ID, `LIMIT` `min(k, max_chunks)` (≤ 8).
- Filters: document `lifecycle='live'`; the document's `published_version_id` version with status
  `published`; chunk status `completed` with an embedding; embedding model and dimension equal to
  the configured ones; and `(visibility = 'shared' OR owner = caller)`. **There is no similarity threshold:** "no evidence" means zero usable hits (rows with blank or duplicate excerpts or failing validation are dropped after the query). Ingestion never writes `visibility`, so every
  ingested document is private to its uploader unless changed by hand in SQL.
- Excerpts are clipped to `max_excerpt_chars` (3000) each and `max_evidence_chars` (24000) per
  attempt; blank and duplicate excerpts are dropped.
- The index is HNSW with cosine ops; whether the planner uses it for this filtered query was not
  verified.

Owner scoping is applied in SQL before ranking, and the runtime database role has SELECT-only access
to document tables.

## Failure handling and recovery

| Cause | Failure category |
|---|---|
| Budget or deadline exceeded | `budget_exhausted` |
| Provider returned an unusable response | `provider_protocol` |
| Citation or output validation failed (including empty answer) | `invalid_citations` |
| The query vector handed to the index was not exactly 1024 finite numbers (a defensive check; corrupt chunk rows are logged and skipped, not failed) | `index_integrity` |
| Provider rejected the request (non-transient validation) | `provider_rejected` |
| Dependency unavailable (Bedrock, database) | `service_unavailable` |
| Lease lost mid-attempt, or lease expired | `interrupted` |
| Client disconnected | `cancelled` |
| Anything else | `internal` |

- **Failure intents.** `_fail` first records an in-process intent (buffer capacity 1024, FIFO
  eviction, keyed by run) and then tries to write the failure. If the write cannot be confirmed
  (database outage or timeout), the stream's `failed` event carries `persistence_pending=true` and
  `retry_available=false`; the intent is replayed by the reconciliation loop (every 5 s when healthy, backing off while the database is down) and whenever
  that conversation's turn is submitted, retried or read through `GET …/turns/{id}`. A process loss
  or buffer eviction falls back to lease expiry.
- **Lease expiry.** The lease is set once at admission and never renewed. Expiry is applied lazily:
  whenever a request touches the conversation (admission, retry, detail, history, turn read,
  thread-feedback write) or retention fences it. There is no periodic sweeper, so a stranded turn
  keeps returning 409 `conversation_busy` until the lease lapses (up to 150 s), then becomes
  `interrupted` and retryable.
- **Never downgraded.** A run that is already completed is never rewritten as failed, even when the
  commit acknowledgement was lost and the client saw `failed`. Clients must reconcile
  ([API reference](../reference/api.md#turn-admission-and-idempotency)).
- **User retry.** Every persisted failure category is retryable by the user, but only for the
  conversation's newest attempt and only with a fresh idempotency key and `expected_run_id`.

## Retention

[`db/retention.py`](../../services/chat/src/horizon_chat/db/retention.py) `ConversationRetention`:

- Candidates: conversations whose `last_activity_at` is older than `retention_days` (30) with no
  unexpired lease, plus any already marked `purging`. Only admitting a turn or retry renews
  `last_activity_at`; reads and feedback do not.
- A session-level PostgreSQL advisory lock allows one purger at a time; each pass handles up to `maintenance_batch_size` (100) conversations and immediately runs another pass when the batch was full and at least one conversation was purged.
- The whole batch is first marked `purging` (invisible to the API); then per conversation the LangGraph checkpoint thread is deleted (bounded by the statement timeout) and the conversation row is deleted, which cascades to turns,
  messages, runs, retry requests and feedback. **Users, documents, chunks, original objects and
  ingestion state are not touched.**
- A conversation whose data is inconsistent is deferred behind the others and the pass continues; an outage or checkpoint timeout aborts the pass and the loop backs off. There is no persisted schedule: the first pass
  runs at process start, and the time predicate plus leftover `purging` rows provide catch-up.
- The pass is skipped while readiness fails.

## State owned and consumed

Writes: `users` (creates rows lazily), `conversations`, `turns`, `messages`, `agent_runs`,
`retry_requests`, `message_feedback`, `thread_feedback`, and the `langgraph` checkpoint schema.
Reads only: `documents`, `document_versions`, `document_chunks`. Details and grants:
[Data model](../reference/data-model.md).

## Configuration

Essentials: `ENVIRONMENT_NAME`, `BIND_HOST`, `BIND_PORT`, `FRONTEND_ORIGIN`, `GOOGLE_CLIENT_ID`,
`AWS_REGION`, `MAIN_MODEL_ID`, `UTILITY_MODEL_ID`, `EMBEDDING_MODEL_ID`, `DATABASE_DSN`,
`CHECKPOINT_DATABASE_DSN`. Everything else is policy in
[`config/base.yaml`](../../config/base.yaml) and
[`config/services/chat.yaml`](../../config/services/chat.yaml). Inventory and precedence:
[Configuration reference](../reference/configuration.md#chat-service).

## Development and testing

```zsh
uv run --locked pytest services/chat/tests/unit
```

Integration tests (`services/chat/tests/integration`) need a disposable PostgreSQL with pgvector and
no AWS access; see [Testing](../guides/testing.md). Test doubles live in
[`tests/horizon_chat_testing`](../../services/chat/tests/horizon_chat_testing): a scripted chat model
that replays `AIMessage`s or exceptions and a controlled agent for stream tests. Change recipes:
[Development guide](../guides/development.md#change-recipes).

## Deployment and scaling

- Image `horizon-chat` (port 8080), runs as uid 10001 under `tini`. The Dockerfile health check calls
  `/health`; Compose overrides it with `/ready`. The image contains `config/` and sets
  `HORIZON_CONFIG_DIR=/app/config`.
- It is a single uvicorn process. Concurrency per conversation is one active turn (database lease
  under a row lock); there is no per-user or global turn limit, request-size limit or rate limit in
  the application.
- Bedrock calls use synchronous boto3 clients on executor threads, so the default thread pool bounds
  provider concurrency.
- Running several replicas is not documented or tested here. The design relies on the database for
  coordination (row lock plus lease for turns, advisory lock for retention), but the failure-intent
  buffer is per process, so a replica's pending failures are repaired only by that replica or by
  lease expiry.
- Deployment order and procedures: [Deployment](../guides/deployment.md).

## Operations

Signals: [Observability](../operations/observability.md). Failure scenarios:
[Troubleshooting](../operations/troubleshooting.md).

## Implementation references

[`application/submit_turn.py`](../../services/chat/src/horizon_chat/application/submit_turn.py),
[`application/_turn_stream.py`](../../services/chat/src/horizon_chat/application/_turn_stream.py),
[`db/runs.py`](../../services/chat/src/horizon_chat/db/runs.py),
[`db/ledger.py`](../../services/chat/src/horizon_chat/db/ledger.py),
[`genai/horizon_agent/`](../../services/chat/src/horizon_chat/genai/horizon_agent),
[`bootstrap/supervisor.py`](../../services/chat/src/horizon_chat/bootstrap/supervisor.py).
