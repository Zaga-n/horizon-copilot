# chat-conversations Specification

## Purpose
Provide an owned, durable conversation record and an event stream that a later frontend can use to display live assistant turns and complete message history.

## Requirements

### Requirement: Owned conversations
The service SHALL create conversations with stable IDs, UTC `created_at` and `updated_at` timestamps, an owner identity, and a lifecycle status. It SHALL authorize every read, write, and stream against that owner before accessing agent state.

#### Scenario: Another user's conversation
- **WHEN** a caller supplies a conversation ID owned by someone else
- **THEN** the service rejects access without reading its messages or checkpoints

### Requirement: Verified identity with local development mode
The backend SHALL accept a Google ID token sent as Authorization bearer after frontend Google sign-in, verify its signature, issuer, configured application audience, and expiry, and use the stable subject identifier rather than email to associate users with conversations. Protected APIs SHALL not require a custom developer API key or accept a Google API access token as the identity credential. A separately configured local development identity mode SHALL be available only on a loopback-bound deployment; production configuration SHALL reject that mode.

#### Scenario: Invalid identity token
- **WHEN** a request presents an expired token or one issued for a different application audience
- **THEN** the service denies access before reading user data

#### Scenario: Google sign-in bearer
- **WHEN** the future frontend sends a valid Google ID token for the configured OAuth client as a bearer
- **THEN** protected APIs identify the verified subject without a separate developer API key

### Requirement: Durable ordered messages
The service SHALL store each visible user and assistant message with a stable ID, conversation ID, turn ID, role, content, status, UTC `created_at` and `updated_at`, and a deterministic order. Assistant messages SHALL also retain structured citations when used; internal tool messages and hidden reasoning SHALL not appear as user-facing history.

#### Scenario: Completed turn appears in history
- **WHEN** a turn finishes successfully
- **THEN** history returns the user message and one completed assistant message in order with its citations and timestamps

### Requirement: Structured answer citations
For an answer using indexed evidence, the service SHALL return inline citation markers in the answer and a separate ordered `sources` collection. Each source SHALL identify its document, immutable document version, title with filename fallback, original filename, file type, and available page/section heading, with a stable source ID matching the markers. Cross-boundary chunks SHALL preserve the full contributing page/section locator collection in addition to any primary display locator. These basic v1 values SHALL come from retrieved source metadata rather than be invented by the model. The same source collection SHALL be persisted with the assistant message and returned by history. If no indexed evidence was used, `sources` SHALL be empty and the answer SHALL not fabricate document citations.

#### Scenario: Answer cites a PDF page
- **WHEN** an answer uses a chunk from page 12 of an indexed PDF
- **THEN** the final stream event and stored assistant message identify that document version and page 12 through the same citation marker

#### Scenario: Basic metadata in references
- **WHEN** retrieved evidence includes filename, title, file type, and page/section metadata
- **THEN** the final sources and stored message history preserve those values for displaying references

#### Scenario: General answer without indexed evidence
- **WHEN** the assistant answers a broad Horizon question without retrieved content
- **THEN** its `sources` collection is empty

#### Scenario: Summarization changes agent context
- **WHEN** old turns are summarized for the agent
- **THEN** previously completed visible messages remain intact in application history

### Requirement: Durable agent continuity
The service SHALL resume a conversation using the same stable conversation ID as its agent thread ID. Application history and agent state SHALL have distinct owners and SHALL not be treated as interchangeable records.

#### Scenario: Follow-up after restart
- **WHEN** the service restarts and an owner sends a follow-up in an existing conversation
- **THEN** the agent has the prior durable conversational context and the history API returns the original messages

### Requirement: One active turn and idempotent submission
The service SHALL prevent concurrent agent runs on the same conversation. It SHALL accept a client turn idempotency key so a retry does not create a duplicate user message or assistant answer.

#### Scenario: Duplicate request
- **WHEN** a caller repeats a turn request with the same idempotency key
- **THEN** the service returns the existing turn outcome or active-turn status without invoking the agent twice

### Requirement: Streaming turn contract
The service SHALL offer an async, ordered event stream with a stable turn ID and event types for turn start, meaningful agent or retrieval progress, assistant text deltas, citation metadata, and exactly one terminal outcome while the connection remains open. It SHALL expose only the final answer attempt as user-visible text deltas and SHALL persist the final answer before emitting a successful terminal event.

#### Scenario: RAG answer is streamed
- **WHEN** the agent retrieves evidence and generates an answer
- **THEN** the caller receives progress events, ordered answer deltas, citation metadata, and a completion event containing the persisted assistant message ID

#### Scenario: Turn fails after stream start
- **WHEN** the model or database cannot complete a streamed turn
- **THEN** the stream ends with a sanitized failure event containing a retryable turn reference, and history marks the assistant attempt as failed, including any already streamed partial text as failed content rather than a completed answer; if bounded persistence retries cannot reach PostgreSQL, the failure event identifies `persistence_pending=true` and disables retry until reconciliation confirms the authoritative state

### Requirement: Retry a failed answer
The service SHALL let an authorized user retry a failed turn without creating a second user message or silently appending a partial assistant answer to agent context. A retry SHALL create a new assistant attempt linked to the original turn, with its own status and timestamps.

#### Scenario: Retry after partial stream failure
- **WHEN** a user retries a failed assistant attempt
- **THEN** the service generates a new answer attempt for the original user message and history preserves the earlier failed attempt as failed

### Requirement: Per-answer feedback
The service SHALL accept one like or dislike value per user for each completed assistant message and allow that user to change or clear it. Feedback SHALL include an optional bounded plain-text comment for dislike, be stored with the user ID, message ID, and UTC creation/update timestamps, and be returned with that user's message history. Selecting dislike SHALL not require a comment. Changing to like SHALL clear a dislike-only comment. The server SHALL derive the target execution from the persisted assistant message, never trust client-supplied execution identifiers, and reject feedback on pending or failed attempts.

#### Scenario: Change a dislike to a like
- **WHEN** the owner changes feedback on a completed assistant message from dislike to like
- **THEN** history shows the like and no duplicate active feedback record exists

### Requirement: Thread feedback
The service SHALL accept one editable conversation feedback record per author/conversation with an optional like/dislike rating and optional bounded plain-text comment, requiring at least one nonempty value. Text-only feedback SHALL remain supported. It SHALL associate feedback with the author and UTC creation/update timestamps, and record the server-derived latest accepted run and message-order boundary at submission or edit so the evaluated conversation context is explicit. An empty conversation SHALL use a null run and empty boundary. This context SHALL not claim that the feedback rates one particular answer. An author SHALL be able to edit or delete their feedback; another user SHALL not read or mutate it without conversation access.

#### Scenario: Submit thread feedback
- **WHEN** a conversation owner submits general feedback on the thread
- **THEN** the feedback is stored separately from chat messages and can be retrieved and edited by that owner

### Requirement: Readable history API
The service SHALL support listing an owner's conversations and cursor-paginated, ordered messages for a conversation. Responses SHALL include message statuses so interrupted turns are distinguishable from completed answers.

#### Scenario: History after reconnect
- **WHEN** a caller reconnects after a stream interruption
- **THEN** it can obtain the stored turn status and messages without relying on the lost stream

### Requirement: Configurable conversation inactivity retention
The service SHALL run daily cleanup of conversations inactive beyond a configurable period, defaulting to 30 days, measured from their last accepted conversational activity. Cleanup SHALL remove the conversation's messages, citations stored with them, answer/thread feedback, turn records, and LangGraph thread state, while retaining users and uploaded documents, chunk results, indexes, and ingestion state. History reads and feedback edits SHALL not extend conversational activity. Conversations with an active turn SHALL be protected from cleanup.

#### Scenario: Inactive conversation expires
- **WHEN** a conversation has no accepted conversational activity for more than the configured retention period and no active turn
- **THEN** daily cleanup removes its chat records and agent state while its owner's documents remain available

#### Scenario: Older conversation is still used
- **WHEN** a conversation was created more than 30 days ago but recently accepted a user turn or explicit answer retry
- **THEN** retention uses that recent activity and preserves the conversation

#### Scenario: Feedback does not renew retention
- **WHEN** the owner reads history or edits feedback on an otherwise expired inactive conversation
- **THEN** that operation does not restart the conversation retention period

### Requirement: Recoverable retention execution
Concurrent service replicas SHALL coordinate retention work so a conversation is not purged while a new turn starts. Cleanup interrupted by a service or database failure SHALL be retryable and SHALL not leave a purged conversation accessible through residual agent state. Missed daily work SHALL run after service recovery.

#### Scenario: Crash during purge
- **WHEN** cleanup stops after agent state removal but before application rows are removed
- **THEN** the conversation remains unavailable and cleanup safely resumes without reopening its thread

#### Scenario: New turn races with cleanup
- **WHEN** a user turn and expiration cleanup contend for the same conversation
- **THEN** a valid accepted active turn protects it, or cleanup marks it unavailable before any new agent run begins

### Requirement: Durable execution attribution
Each accepted answer attempt SHALL have a server-assigned run_id linked to conversation_id, turn_id, user_message_id, assistant_message_id, attempt_number, trace_id, status, and UTC start/end timestamps. conversation_id SHALL equal the authorized LangGraph thread_id. Execution attribution SHALL be persisted before agent invocation and remain available in owner-authorized history and turn status even if telemetry is unsampled, export fails, or its retention expires. Each explicit retry SHALL use a new run_id, assistant_message_id, and trace_id while retaining the original turn/user message. Idempotent replay SHALL retain existing attribution. Internal physical model retries SHALL remain child observations of the same run.

#### Scenario: Compare failed and retried answers
- **WHEN** a failed turn is retried and the resulting answer receives feedback
- **THEN** history identifies both runs and feedback joins only the rated assistant message and its own trace

#### Scenario: Trace unavailable
- **WHEN** an answer completes but its trace is not retained by the telemetry destination
- **THEN** its persisted execution attribution and feedback remain readable without claiming that trace data is available

### Requirement: Reconciliation and feedback read contract
The service SHALL expose owner-authorized turn status with active/terminal state, attempt identifiers, safe failure category, and retry availability, and expose conversation detail with saved thread feedback. It SHALL return the existing turn identifiers on idempotent submission replay and reject reuse of a submission key for different input. Explicit retry requests SHALL themselves be idempotent and SHALL reject stale retry targets or an active run rather than produce overlapping attempts. Feedback comments SHALL be limited to 4000 Unicode code points, trim empty text to null, and reject invalid ratings or oversized comments.

#### Scenario: Lost terminal event
- **WHEN** a caller requests status after losing the completion event
- **THEN** the response identifies the saved completed assistant message/run without starting execution

#### Scenario: Conversation feedback context
- **WHEN** an owner submits thread feedback and later adds another turn
- **THEN** stored feedback retains its original run/message boundary until explicitly edited

#### Scenario: Duplicate explicit retry
- **WHEN** the same explicit retry request key is replayed after response loss
- **THEN** it resolves to the same assistant run without another model execution

#### Scenario: Database unavailable at terminal persistence
- **WHEN** terminal writes exhaust bounded retries because PostgreSQL is unavailable
- **THEN** the stream emits one sanitized `failed` event with `persistence_pending=true`, stable attempt identifiers, and no immediate retry availability
- **AND** every already visible partial batch remains durably reserved as pending/failed rather than complete
- **AND** reconciliation after recovery repairs a pending attempt or reports its already committed outcome without downgrading completion; replica/process loss falls back to active-run lease recovery

### Requirement: Invalid text input is rejected, not failed
Chat SHALL reject user text that contains NUL characters (message content, feedback comments) with a validation error before any state change. Database data errors and connection-pool exhaustion SHALL surface as a rejected request or as a service-unavailable response respectively, never as an unclassified internal error.

#### Scenario: Message containing a NUL character
- **WHEN** a user submits a turn whose content contains a NUL character
- **THEN** the API responds with a validation error and no turn, run, or message is created

#### Scenario: Connection pool exhausted
- **WHEN** no database connection becomes available within the pool timeout
- **THEN** the request receives a service-unavailable response and readiness reports unavailable rather than an internal error

### Requirement: Per-item isolation in background maintenance
Retention purges and failure recovery SHALL record a failure on the affected conversation or run and continue with the remaining items. Only a dependency outage SHALL stop a pass. An item that keeps failing SHALL NOT block other items or later turns on unrelated conversations, and SHALL be discoverable by operators through a log event and bounded metric.

#### Scenario: One conversation's checkpoint cannot be deleted
- **WHEN** a retention pass encounters a conversation whose checkpoint deletion fails with an integrity error
- **THEN** the pass purges the other due conversations, the failing conversation is recorded as degraded, and it does not stay first in every later pass

#### Scenario: Recovery intent fails for one run
- **WHEN** failure recovery cannot apply a pending failure to one run because of an integrity error
- **THEN** recovery continues for other runs, and new turns on other conversations are admitted normally

### Requirement: Supervised background loops
The recovery and retention loops SHALL run under a process supervisor that owns their cadence, failure policy, and shutdown. Readiness SHALL report not-ready when a required loop has stopped unexpectedly.

#### Scenario: Retention loop crashes
- **WHEN** the retention loop exits with an unexpected error
- **THEN** the supervisor logs the failure and readiness reports not-ready until the process is restarted

### Requirement: Liveness reflects stopped background loops
The chat process's liveness endpoint SHALL report failure when a required background loop (recovery or retention) has stopped unexpectedly, so the platform restarts the process. While every required loop is running, liveness SHALL report success regardless of dependency outages. A loop that backs off during a dependency outage SHALL NOT count as stopped.

#### Scenario: Retention loop crashes on a defect
- **WHEN** the retention loop exits with an unexpected error
- **THEN** both readiness and liveness report failure until the process restarts

#### Scenario: Database outage during maintenance
- **WHEN** the database is unreachable and the loops back off
- **THEN** liveness keeps reporting success and the loops resume once the database recovers

### Requirement: Missing database privileges are a dependency outage
A database operation refused for insufficient privilege SHALL be classified as a dependency outage in every chat store. It SHALL NOT be classified as an unclassified internal error. API requests SHALL receive the service-unavailable problem, and background loops SHALL back off instead of stopping.

#### Scenario: Runtime grant missing after a migration
- **WHEN** a runtime role lacks a privilege that retention or recovery needs
- **THEN** the loop backs off and keeps running, and recovers without a restart once the grant is applied

### Requirement: Bounded shutdown of background maintenance
On shutdown the chat process SHALL stop its maintenance loops within a configured grace period. One retention iteration SHALL purge at most one batch, and SHALL report whether more work is due so the next batch runs promptly. Work interrupted by shutdown SHALL resume on the next start without loss.

#### Scenario: Shutdown with a large retention backlog
- **WHEN** a stop signal arrives while many conversations are due for purge
- **THEN** the process exits within the grace period, and the remaining conversations are purged after restart

#### Scenario: Backlog drains without waiting for the interval
- **WHEN** an iteration purges a full batch and more conversations are due
- **THEN** the next iteration starts without waiting for the normal retention interval
