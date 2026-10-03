## Purpose

Answer questions about Horizon Europe projects and their documents with bounded retrieval, clear uncertainty, and defenses against instructions embedded in untrusted content.

## ADDED Requirements

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
