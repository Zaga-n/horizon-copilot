## Why

The planned chat and ingestion APIs need a usable interface for signing in, asking streamed questions, managing source documents, and providing feedback. Users need clear progress and recovery controls, while persisted feedback must remain attributable to the answer execution that produced it.

## What Changes

- Add a responsive authenticated workspace with Google sign-in, conversation navigation, a streaming chatbot, citations, and recoverable failed answers.
- Add a document panel with PDF/.docx upload, stage-aware spinners and committed chunk counters, ingestion retry, and coordinated deletion.
- Add per-answer like/dislike with optional dislike comments and a separate conversation rating/comment control.
- Preserve server-issued conversation, turn, run, and message identifiers in client state and feedback requests; reconcile history after interrupted streams.
- Define accessible loading, empty, error, mobile, and expired-session states and a local frontend launch path.

## Capabilities

### New Capabilities

- `frontend-workspace`: Login, authenticated navigation, responsive layout, and session lifecycle.
- `frontend-chat`: Streaming messages, history, citations, failed-answer retry, and distinct answer/conversation feedback controls.
- `frontend-documents`: Upload, ingestion progress, selective retry, and document deletion UI.

### Modified Capabilities

None in this change. Revisions to the unimplemented backend, ingestion, and telemetry contracts are captured in their existing changes rather than duplicated here.

## Impact

Creates a TypeScript React web frontend consuming `horizon-agent-backend` and `document-ingestion-pipeline`. Those changes own shared DB migrations, feedback persistence, execution correlation, and fixed-window chunking. `local-observability-stack` owns trace projection; this change owns frontend runtime configuration and local launch documentation. Automated evaluation, feedback export, and an analytics dashboard are deferred; durable joins needed for future evaluation are included in the backend plan.
