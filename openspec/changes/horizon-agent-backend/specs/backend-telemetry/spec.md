## Purpose

Expose useful and privacy-conscious operational evidence for the chat backend so local and future environments can diagnose requests, retrieval, models, and streaming latency.

## ADDED Requirements

### Requirement: Correlated traces and logs
The backend SHALL emit one dedicated trace for each bounded answer-attempt run, including a distinct trace for each explicit retry, with child observations for physical model attempts, tool executions, retrieval, and embedding calls. Structured logs SHALL carry the active trace and request or turn identifiers when available, without recording prompts, retrieved text, credentials, or answer bodies by default.

#### Scenario: Diagnose a failed search
- **WHEN** retrieval fails during a turn
- **THEN** an operator can correlate the turn's failure log with its retrieval and agent trace without seeing document content in ordinary telemetry

### Requirement: Useful bounded metrics
The backend SHALL emit request and error latency metrics plus model duration, token usage when provided, first model chunk latency, first user-visible answer chunk latency, retrieval duration and result count, tool duration, and model/tool call fan-out per turn. Metric labels SHALL have bounded cardinality and SHALL not contain user, conversation, prompt, or document identifiers.

#### Scenario: Slow first answer token
- **WHEN** a streamed answer is delayed before its first visible text delta
- **THEN** an operator can distinguish model first-chunk delay from retrieval and overall agent time

### Requirement: Privacy-safe telemetry failure behavior
Telemetry export failure SHALL not alter the chat outcome. Diagnostic content capture for a GenAI backend SHALL be disabled by default and, if enabled, SHALL be explicitly scoped and redacted before export.

#### Scenario: Collector unavailable
- **WHEN** the telemetry destination is unavailable
- **THEN** chat processing continues while normal service logs retain enough bounded diagnostic context

### Requirement: Bounded retention diagnostics
Daily retention SHALL emit correlated completion/failure logs, bounded maintenance traces, and low-cardinality purge-count, duration, and failure metrics. These signals SHALL distinguish successful cleanup from retryable partial cleanup without exposing conversation content.

#### Scenario: Checkpoint cleanup fails
- **WHEN** a retention batch cannot finish removing an expired thread
- **THEN** operators can identify the retryable cleanup failure without the service reporting that thread as fully purged

### Requirement: Execution correlation for evaluation
Each answer-attempt root SHALL carry conversation_id/thread_id, turn_id, run_id, user_message_id, assistant_message_id, and attempt_number matching persisted execution attribution. Retained descendant spans SHALL be attributable to that run. Retrieval observations SHALL include bounded chunk IDs, immutable document-version IDs, scores/ranks, and retrieval configuration identity; model observations SHALL identify model and prompt configuration versions, latency, and provider-reported token usage when available. Identifiers SHALL remain span/log attributes rather than metric labels. Unknown usage or cost SHALL not be reported as zero; estimated cost SHALL identify its pricing source/version. Diagnostic content capture SHALL remain opt-in and redacted, and SHALL not require or promise hidden chain-of-thought capture.

#### Scenario: Join a disliked answer to retrieval
- **WHEN** an owner dislikes a completed answer and its trace is retained
- **THEN** its persisted message/run identifies the exact trace, retrieved version/chunk references, and physical model attempts for that answer rather than another retry

#### Scenario: Provider omits usage
- **WHEN** a provider response lacks token usage or pricing data
- **THEN** telemetry marks the measurement unavailable rather than fabricating token or cost values
