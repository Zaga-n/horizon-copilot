# horizon-agent Specification

## Purpose
Answer questions about Horizon Europe projects and their documents with bounded retrieval, clear uncertainty, and defenses against instructions embedded in untrusted content.

## Requirements

### Requirement: Horizon scope and ambiguity handling
The assistant SHALL answer questions within the broad scope of Horizon Europe projects, programmes, proposals, work packages, deliverables, and related management. It SHALL give a brief scope response for unrelated requests. It SHALL ask one clarifying question when a bare or ambiguous project name might refer to a Horizon project, rather than classifying it as unrelated.

#### Scenario: Ambiguous project name
- **WHEN** the user asks about a name without enough context to tell whether it is a Horizon project
- **THEN** the assistant asks for a project identifier or Horizon context and does not fabricate project facts

#### Scenario: Unrelated request
- **WHEN** the user asks a clearly unrelated question
- **THEN** the assistant briefly explains its Horizon scope without using retrieval

### Requirement: Evidence-aware answers
The assistant SHALL use retrieval for factual claims about a named project, document-specific claims, and uncertain claims; it SHALL favor retrieval whenever it can improve a Horizon answer. It SHALL cite only returned source IDs for material document claims, with markers tied to the final answer's structured sources, and SHALL say when indexed material does not support an answer. It SHALL not present model general knowledge as a quotation from a document.

#### Scenario: Specific work package question
- **WHEN** a user asks about a work package in an indexed Horizon document
- **THEN** the assistant searches the index and answers with traceable source references

#### Scenario: No relevant evidence
- **WHEN** searches return no sufficiently relevant chunks
- **THEN** the assistant states that it could not verify the project-specific detail and asks for a document or identifier if useful

#### Scenario: Named project claim
- **WHEN** a user asks for facts about a named Horizon project
- **THEN** the assistant searches the index before making project-specific claims, even if the model might know the project name

### Requirement: Bounded retrieval
The assistant SHALL expose only one retrieval tool to the main agent. A retrieval call SHALL rewrite the question with conversation context into a bounded search query, search the indexed chunks without modifying them, and return a bounded set of content and source metadata. The assistant SHALL be able to request a different query or bounded result count for a second search within per-turn limits. In v1, basic source metadata SHALL accompany results and citations; the agent SHALL not supply metadata filters or automatically narrow retrieval by inferred project/work-package metadata. Mandatory authorization, publication, and deletion visibility rules SHALL still apply.

#### Scenario: First search is incomplete
- **WHEN** the first search yields insufficient evidence and budget remains
- **THEN** the assistant can retry with a changed query or result count and receives only bounded, deduplicated results

### Requirement: Untrusted content isolation
The assistant SHALL treat user input and retrieved document chunks as untrusted. A separate model-based check SHALL classify user input for scope and prompt injection before agent work, with an ambiguity outcome distinct from rejection. Retrieved text SHALL be treated as evidence only, never as instructions to the assistant or its tools.

#### Scenario: Injection inside a retrieved chunk
- **WHEN** a chunk contains instructions to ignore policy or reveal hidden data
- **THEN** those instructions are not followed and no privileged content is disclosed

#### Scenario: Guardrail uncertainty
- **WHEN** the guardrail cannot confidently distinguish a project name from unrelated content
- **THEN** the assistant asks for clarification instead of issuing a hard scope rejection

### Requirement: Bounded and terminal turns
Each turn SHALL have configurable finite limits for main-agent model calls, retrieval calls, query rewrite calls, result count, token output, retries, and wall-clock duration. Transient model and tool failures SHALL be retried with bounded backoff. When a required tool or model remains unavailable, the service SHALL give a brief user-facing service-unavailable outcome and record diagnostic details only in developer telemetry. It SHALL not ask the user to inspect logs or disclose internal errors.

#### Scenario: Tool budget exhausted
- **WHEN** the assistant wants another search after the search limit is reached
- **THEN** it stops searching and gives a best-supported answer or states its uncertainty

#### Scenario: Transient model failure
- **WHEN** a retryable model failure occurs
- **THEN** the service retries within the configured attempt and time budgets and terminates the turn if recovery fails

#### Scenario: Retrieval service remains unavailable
- **WHEN** bounded tool retries are exhausted during a required project-specific search
- **THEN** the user receives a brief try-again-later outcome, the attempt is marked failed and retryable, and diagnostic details appear only in server telemetry

### Requirement: Indexed-chunk compatibility
The retrieval service SHALL reject incompatible embedding dimensions or missing required document metadata at readiness, and SHALL return stable chunk identifiers, text, title with filename fallback, original filename, file type, document version identifier, and available page/section heading for matching chunks. It SHALL search only published documents the current user may access, including explicitly shared corpus documents, and SHALL exclude deleting/deleted document lifecycles and unpublished candidate chunks.

#### Scenario: Index dimension mismatch
- **WHEN** the stored vectors do not match the configured query embedding dimension
- **THEN** readiness fails with a diagnostic error instead of producing invalid retrieval results

#### Scenario: Another user's private upload
- **WHEN** a user asks about a project whose only indexed document is another user's private upload
- **THEN** retrieval excludes that document and the answer does not reveal its contents or existence

#### Scenario: Document deletion is accepted
- **WHEN** the document owner initiates deletion while object cleanup is still pending
- **THEN** new retrieval excludes its chunks immediately rather than waiting for physical cleanup

### Requirement: Distinct provider rejection outcome
A turn whose model request the provider rejects as invalid (for example an input that exceeds the model's context) SHALL fail with a provider-rejected category that is distinct from service-unavailable. A provider outage, throttle, or access-denied response SHALL remain service-unavailable.

#### Scenario: Context window exceeded
- **WHEN** the model provider rejects a turn's request as invalid input
- **THEN** the turn ends with the provider-rejected failure category, and the stream's terminal event does not claim the service is unavailable

#### Scenario: Provider throttling
- **WHEN** the model provider throttles a turn's requests beyond the retry budget
- **THEN** the turn ends with the service-unavailable failure category

### Requirement: Every agent failure has a named outcome
Every failure raised while an agent turn runs SHALL end the turn with a named failure category:
- model calls;
- query embedding, including a malformed provider response;
- retrieval;
- guardrail classification, including an unparseable structured result;
- citation validation.

No such failure SHALL be reported as an unclassified internal error. An index-integrity fault SHALL be reported as an index-integrity failure, not as an invalid-citation failure.

#### Scenario: Malformed embedding response
- **WHEN** the embedding provider returns a successful response without a usable vector during retrieval
- **THEN** the turn ends with a named provider failure category and the run is marked failed

#### Scenario: Guardrail returns unparseable output
- **WHEN** the guardrail model's structured output cannot be parsed
- **THEN** the turn ends with a named failure category rather than an internal error

### Requirement: Failure classes are never folded into each other
A turn failure SHALL keep the class of its cause through retrieval, tools and the model runner:
- **unavailable:** outage, throttle, access denied, unknown or unavailable model identifier;
- **rejected:** the provider rejects the input itself;
- **invalid output:** a 2xx response that cannot be parsed, including a malformed embedding body and an unparseable guardrail result.

Only the unavailable class SHALL be retried inside a turn. A misconfigured model identifier SHALL end turns as service-unavailable, not provider-rejected.

#### Scenario: Unknown model identifier
- **WHEN** the provider reports that the configured model identifier is invalid in the configured region
- **THEN** the turn ends with the service-unavailable category, not provider-rejected

#### Scenario: Embedding provider rejects the query
- **WHEN** query embedding is rejected by the provider as invalid input
- **THEN** the tool is not retried and the turn ends with the provider-rejected category

#### Scenario: Malformed model response
- **WHEN** the chat model returns a successful response whose body cannot be parsed
- **THEN** the turn ends with a named provider-protocol category, not an internal error, and is not retried

### Requirement: Retrieval failures are classified and isolated per row
A retrieval connection-pool timeout or a network error SHALL be classified as unavailable. A stored chunk row whose source or locator cannot be decoded SHALL be skipped and logged as an index-integrity fault, and the remaining evidence SHALL be used. A locator field this service does not recognise SHALL NOT make a row undecodable.

#### Scenario: Pool exhausted under load
- **WHEN** retrieval cannot obtain a database connection within its timeout
- **THEN** the search is retried as transient and, if the retry budget is exhausted, the turn ends as service-unavailable

#### Scenario: Ingestion adds a locator field
- **WHEN** indexed chunks carry a locator key this service does not know
- **THEN** retrieval returns those chunks as evidence and no turn fails as internal

### Requirement: Lost turn lease ends as interrupted
When a turn's lease has expired and another actor has taken over or failed the run, the original stream SHALL end with the interrupted category. It SHALL NOT be reported as an internal error.

#### Scenario: Slow client outlives the lease
- **WHEN** a stream's run lease expires before the answer is persisted
- **THEN** the stream ends with the interrupted category and the run's stored terminal status is unchanged by the late writer
