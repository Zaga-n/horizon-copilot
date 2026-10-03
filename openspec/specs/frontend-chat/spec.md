# frontend-chat Specification

## Purpose
Display durable streamed conversations with usable recovery, citations, and independently scoped answer and conversation feedback.

## Requirements

### Requirement: Conversation history and navigation
The frontend SHALL allow creating and selecting owned conversations and loading ordered paginated history. It SHALL distinguish pending, completed, and failed attempts, preserve failed-attempt history after retries, and retain server identifiers rather than infer identity from message position.

#### Scenario: Reopen a conversation
- **WHEN** an owner selects a prior conversation
- **THEN** ordered messages, sources, statuses, and saved answer/conversation feedback appear

### Requirement: Incremental assistant rendering
The frontend SHALL render ordered answer deltas as they arrive, show meaningful progress separately, and render safe markdown and citations without exposing internal reasoning or tool payloads. It SHALL prevent overlapping submissions in one conversation, preserve the user's scroll position when reading earlier messages, and offer a jump to the latest content. Success SHALL require the persisted completion event or authoritative completed history.

#### Scenario: Receive answer deltas
- **WHEN** a stream delivers several text deltas before completion
- **THEN** visible answer text grows before the completion event and feedback becomes available only for the completed assistant message

#### Scenario: Read earlier messages while streaming
- **WHEN** the user scrolls away from the latest message
- **THEN** new deltas do not force scrolling and a latest-content control becomes available

### Requirement: Interrupted-stream reconciliation and explicit retry
The frontend SHALL mark partial failed text clearly, expose Retry for server-confirmed retryable failures, and reconcile turn/history status after disconnect or unknown submission outcome. Explicit retry SHALL target the original turn, create a new assistant attempt, and reuse the original user message. The frontend SHALL not blindly replay a request or retry an active attempt.

#### Scenario: Partial response fails
- **WHEN** an answer fails after visible deltas
- **THEN** its partial text is labelled failed and Retry starts a new attempt without duplicating the user message

#### Scenario: Completion event is lost
- **WHEN** the connection drops after the server has saved a completed answer
- **THEN** history reconciliation replaces the local provisional state with that completed answer without generating another answer

### Requirement: Source references
Completed answers SHALL display citation markers and an accessible source list using server metadata. Deleted sources SHALL be marked unavailable while saved answers remain readable.

#### Scenario: Deleted citation source
- **WHEN** the owner opens an old answer whose document has been deleted
- **THEN** the reference still identifies the original document/page and reports that the source is unavailable

### Requirement: Answer rating and optional dislike comment
Each completed assistant message SHALL have like/dislike controls with the saved selection visible. Dislike SHALL save the rating immediately and offer an optional comment; dismissing the comment input SHALL preserve the dislike. Users SHALL be able to edit/clear feedback. Save failures SHALL be visible and retain comment drafts; failed or streaming attempts SHALL not accept ratings.

#### Scenario: Dislike without comment
- **WHEN** a user selects dislike and dismisses the optional comment dialog
- **THEN** the dislike remains saved against that assistant message

#### Scenario: Feedback request fails
- **WHEN** saving a comment fails
- **THEN** the frontend retains its draft, reports the failure, and offers resubmission without claiming it was saved

### Requirement: Separate conversation feedback
A labelled conversation feedback control SHALL appear in the conversation header, separately from answer actions. It SHALL support an optional like/dislike rating and optional general comment with at least one present, including text-only feedback, and allow editing or deletion. Client requests SHALL address the conversation; server-derived execution context SHALL never be supplied as trusted client attribution.

#### Scenario: General conversation comment
- **WHEN** a user opens the header feedback control and submits text without a rating
- **THEN** feedback is saved for the conversation separately from individual answer ratings
