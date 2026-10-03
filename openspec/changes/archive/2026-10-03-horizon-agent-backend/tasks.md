## 1. Service foundation

- [x] 1.1 Create a Python workspace with chat, ingestion, and one-shot containerized migrations deployables, one shared `app` model package and one migration-service-owned Alembic history, pinned dependencies, lint/type/import-boundary checks, and CI; verify a clean install and quality command succeed.
- [x] 1.2 Add typed chat settings for PostgreSQL, Bedrock models, embedding dimensions, agent limits, Google OIDC verification, local identity mode, retention/daily maintenance scheduling, and OTLP; verify invalid values and production use of local identity fail startup validation.
- [x] 1.3 Build async FastAPI bootstrap with resource cleanup and readiness; verify a lifecycle test opens and closes DB/model dependencies and returns unready on missing schemas.

## 2. Database ownership and conversation ledger

- [x] 2.1 Add the shared Alembic baseline for `app.users`, conversations, messages, answer/thread feedback with rating/comment checks and thread context, turns/agent_runs and idempotent retry mappings with attribution constraints/indexes, and document/version/chunk read models with constraints and indexes, last_activity_at, purge lifecycle, and maintenance scheduling/lease records; verify fresh-database replay and 1024-dimension validation.
- [x] 2.2 Restrict Alembic metadata/autogenerate to `app` and schema-qualify its version table; verify a LangGraph table produces no Alembic diff.
- [x] 2.3 Implement the dedicated migration service/container that applies the shared app Alembic history and then async LangGraph checkpoint setup using the `langgraph` search path and migration role; verify installed-container fresh replay, safe reruns, nonzero exit on failure, and checkpoint tables only in `langgraph`.
- [x] 2.4 Implement chat runtime stores and role grants for owned conversation reads/writes, idempotent turn creation, active-turn leases, feedback, and message ordering; verify ownership, duplicate submission, feedback upsert/delete, and concurrent-turn integration tests.
- [x] 2.5 Implement checkpoint/display-ledger recovery for interruption, failed assistant attempts, and retry; verify a simulated crash and retry do not duplicate user input or label partial text as complete.
- [x] 2.6 Implement daily async retention with persisted scheduling, replica lease, last accepted activity (30-day configurable default), conversation fencing, checkpointer thread deletion, and chat-record cascade; verify active-turn races, crash between stores, missed-run recovery, and preserved users/documents/chunks/ingestion state.

## 3. Agent and retrieval

- [ ] 3.1 Probe pinned `init_chat_model` Bedrock Converse integration with Terra/Luna for async streaming, client-side tool calls, structured output, token usage, and low/none reasoning settings; record the working configuration and verify an AWS smoke test.
- [x] 3.2 Implement read-only Titan query embedding and parameterized pgvector retrieval with caller visibility filtering, bounded rewrite, `k`, excerpts, scores, deduplication, and basic source metadata (title, filename, type, page/section heading), without agent metadata-filter arguments; verify private-document isolation, deleting/deleted-source exclusion, empty, relevant, and incompatible-index cases.
- [x] 3.3 Build the `create_agent` Horizon prompt and one `rag_search` tool, with source validation and a second bounded search option; verify every named-project factual answer searches, cites retrieved chunks, and treats unsupported claims as uncertain.
- [x] 3.4 Add Luna input guardrail with in-scope, ambiguous, out-of-scope, and injection outcomes; verify bare project names ask for clarification and adversarial input cannot call retrieval or expose hidden instructions.
- [x] 3.5 Add summarization, model retry, tool retry with backoff, main-loop model/tool call limits, and outer physical-call/deadline budget; verify long chats, retry exhaustion, and budget exhaustion end with a friendly response and developer-only diagnostics.
- [ ] 3.6 Test malicious instructions in retrieved chunks and citation spoofing; verify only valid chunk metadata can become sources and the agent ignores document instructions.

## 4. API and streaming

- [x] 4.1 Implement Google ID-token Authorization bearer verification using stable `sub` and configured public OAuth client audience without a developer API key, local loopback identity mode, and owned create/list/history endpoints; verify issuer/audience/expiry rejection, owner isolation, and UTC ordered records.
- [x] 4.2 Implement SSE turn endpoint with progress, real final-answer deltas, structured citation sources, and one terminal event; verify source markers and basic metadata match retrieved chunks and persisted history, while provisional/internal messages are never exposed as final answer text.
- [x] 4.3 Persist completion before successful terminal emission and failed partial attempts before failure events; implement retry endpoint and verify reconnect history, duplicate client key, cancellation, post-start failure behavior, and bounded DB-outage fallback with `persistence_pending` plus authoritative reconciliation.
- [x] 4.4 Add answer like/dislike with optional dislike comments and thread rating/comment endpoints with upsert, clear/delete, timestamps, and owner checks; verify feedback is stored separately from conversation messages and returned to the owner.

## 5. Observability and release gates

- [x] 5.1 Add one OTel bootstrap, model-attempt callback, tool/retrieval/embedding/agent spans, and bounded service/GenAI and retention metrics; verify one trace tree per run, distinct explicit-retry traces, physical retries, model/agent first-chunk timing, maintenance failure diagnostics, and no per-token spans.
- [x] 5.2 Add structured JSON logging with trace and turn correlation plus default content exclusion; verify successful and failed event examples contain no prompts, chunks, secrets, or high-cardinality metric labels.
- [ ] 5.3 Run focused integration tests and a real Bedrock final-phase streaming/checkpoint smoke test, and document deployment order and provider limits; verify all capability scenarios, including citations, feedback, retryable failure, retention recovery, and document preservation, pass.

## 6. Evaluation attribution and frontend compatibility

- [x] 6.1 Persist dedicated run/root trace attribution before invocation and update reserved assistant/run rows together on terminal outcomes; verify same-turn retries have different run/message/trace IDs, internal provider retries stay children, and crash recovery retains attribution.
- [x] 6.2 Expose owned conversation detail and turn reconciliation with run metadata and idempotent explicit retry; verify lost completion, stale retry rejection, key/input conflicts, and duplicate retry replay.
- [x] 6.3 Enforce server-derived answer feedback joins, dislike comment limits, thread rating/text checks and context snapshots; verify forged client IDs cannot redirect feedback and later turns do not rewrite prior thread-feedback context.
- [x] 6.4 Verify answer-to-trace joins across history/feedback and retention with unsampled traces/export outage; verify purging cascades run/retry/feedback records and leaves documents intact.
- [x] 6.5 Configure exact frontend-origin CORS and required authorization/idempotency headers; verify browser preflights and rejected unapproved origins.

## Verification boundary for this implementation

The user explicitly deferred AWS online verification. Tasks 3.1 and 5.3 remain
unchecked for their real Bedrock smoke checks. Task 3.6 also remains unchecked
for live adversarial-model obedience: offline tests cover untrusted evidence
framing, citation spoof rejection, source metadata validation, and no tool access
in final inference, but scripted responses do not establish stochastic model
obedience. The offline SDK tests validate Terra/Luna request fields, structured
output, usage parsing, and incremental stream parsing without contacting AWS.

Local verification covers PostgreSQL ownership/admission/retry/feedback,
checkpoint interruption, retention fencing/cascades/document preservation,
stream cancellation, bounded persistence failure and repair, uncertain completion
acknowledgements, unsampled attribution, export failure, and content exclusion.
Full local Compose/Collector deployment and ingestion implementation belong to
`local-observability-stack` and `document-ingestion-pipeline`.
