## Context

Only planning artifacts exist. Chat uses Google ID-token bearer authentication and POST streams; ingestion offers multipart acceptance and owner-scoped status polling. See proposal.md and the three frontend specs for behavior. Backend and ingestion remain separate deployables with one shared application migration history.

## Goals / Non-Goals

**Goals:** a coherent workspace, responsive streaming, truthful progress, and recovery driven by durable server records.

**Non-Goals:** owning agent/checkpoint state, implementing chunking in the browser, exposing observability consoles to users, automated evaluation, or interpreting inferred dissatisfaction as explicit feedback.

## Decisions

### Frontend architecture and integration

Use a TypeScript React single-page application with Vite, feature directories for identity, conversations/chat, documents, and feedback, and a small shared UI/API layer. Static deployment fits two existing independent APIs; server rendering or a new session-owning backend would add an unnecessary server boundary for this authenticated workspace. Pin supported dependencies during implementation. Keep deployment and local dev API base URLs configurable, allowlist the exact frontend origin at both APIs, and never expose Bedrock, DB, MinIO, or OAuth client secrets to the browser. The public OAuth client ID is browser configuration. Use Google Identity Services and keep identity tokens in memory; reload can require sign-in. Clear caches and abort pending work on account change. Local identity bypass is explicit and limited to the existing loopback development contract.

### Visual direction and layout

Use a restrained, light workspace with warm neutral surfaces, dark readable text, one teal accent, generous message spacing, and a narrow comfortable reading column. Use consistent typography and icons; communicate status with text as well as color. Desktop has a collapsible conversation sidebar, a central chat header/transcript/composer, and a document panel opened from a persistently visible Documents action. The header holds conversation feedback; completed answer footers hold like/dislike. Document cards show filename, stage, committed fraction, progress bar, and applicable retry/delete actions. Empty chat offers concise Horizon question starters and a document-upload entry point. Mobile moves navigation and documents to drawers. Respect reduced motion and avoid disruptive layout shifts as text grows.

### Stream and history ownership

Consume POST SSE through fetch and incremental UTF-8 decoding. Handle event boundaries split across reads, multiple events per read, ordered delta sequence numbers, and exactly one terminal transition. Store transient streaming state per selected conversation/run separately from authoritative history. Batched rendering may reduce repeated markdown work but must show text before completion. Sanitize markdown links/content, avoid remounting the transcript on each delta, and do not autoscroll readers viewing earlier messages. Reserve a local provisional entry, then reconcile with server-issued IDs. Switching conversations aborts the local stream and reconciles its server outcome when reopened; it does not assume background continuation. Network loss triggers turn status/history reconciliation, not automatic model retry. Use the same submission key for unknown acceptance; a deliberate failed-answer retry gets a fresh request key and targets the original turn.

### API and feedback contracts

Chat consumes create/list/history, POST /v1/conversations/{id}/turns:stream, GET /v1/conversations/{id}/turns/{turn_id}, and POST /v1/conversations/{id}/turns/{turn_id}:retry. Answer feedback uses PUT/DELETE /v1/messages/{id}/feedback with {rating, comment}; conversation feedback uses PUT/DELETE /v1/conversations/{id}/feedback with {rating, comment}, allowing a null rating for text-only feedback. The server derives attribution from stored message/run records; client-supplied trace IDs never determine the target. Save dislike immediately, then offer a comment dialog with explicit save/dismiss. Changing to like clears a dislike-only comment. Show pending/error states, preserve unsaved text, and restore authoritative values after rejected mutations. Fetch conversation feedback in conversation detail and answer feedback in history. Execution IDs support reconciliation, not a developer-facing UI.

### Document lifecycle

Use multipart upload with an owner-scoped request key; preserve it through uncertain acknowledgments. Poll active document/job status initially every two seconds, with bounded backoff on request failures and visibility-aware pause/resume. Restore polling from the document list after reload. Unknown extraction totals are indeterminate; known totals count committed embeddings and can reach 100% before publication. Show publication as its own stage. Explicit ingestion retry retains successful counts. Delete confirmation names the selected file; accepted deletion remains in a Deleting state until server cleanup finishes. The ingestion API owns canonical filenames in list/status responses, as specified by document-ingestion-pipeline. Read names from server status after acceptance and reload; do not cache filenames in browser storage. Completed deleted tombstones have filename null and leave the active list. No browser object-store access is needed.

### Evaluation readiness

The backend assigns conversation_id (= LangGraph thread_id), turn_id, user_message_id, run_id, assistant_message_id, and trace_id. Each retry gets a distinct run and trace. Feedback joins the exact answer run, and thread feedback records its server-derived conversation context. The UI does not invent analytics for correction, rephrase, or abandonment. These are future derived signals requiring explicit definitions; technical retries are not proof of dissatisfaction.

## Risks / Trade-offs

- Interrupted stream or upload acknowledgment -> reconcile durable status before offering replay.
- Identity expiry or account switch -> reauthenticate, clear owner caches, and preserve only safe request reconciliation metadata for the same account.
- Frequent token updates -> incrementally decode, batch presentation work, and test long markdown/citations during real streaming.
- Chunk completion differs from searchable readiness -> retain the publication stage after all chunks finish.
- Telemetry may be missing -> rely on durable run attribution and never depend on trace export for chat/feedback success.

## Migration Plan

Implement the revised shared backend/ingestion migrations and API contracts before the integrated frontend release. Add the frontend package, local launch instructions, OAuth origin setup, and API CORS configuration. Deploy static assets with environment-specific public configuration; verify login, streamed/retried chat, feedback, ingestion progress, and deletion end to end. Roll back frontend assets independently while preserving backward-compatible API responses and durable data.
