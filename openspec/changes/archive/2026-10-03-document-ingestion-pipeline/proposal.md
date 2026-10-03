## Why

The chat service needs a dependable way to turn uploaded Horizon project documents into cited, searchable chunks. Uploading a file must return promptly while extraction, embedding, retries, and publication continue as durable work that a later frontend can poll.

## What Changes

- Add a separate async FastAPI ingestion service with one multipart upload endpoint: validate and hash PDF/Word files, deduplicate, store originals in MinIO, and commit a pollable job before acknowledging acceptance.
- Run an always-on async worker listening to PostgreSQL notifications, with startup and periodic recovery scans of durable jobs; no MinIO event integration or presigned-upload registration is required.
- Extract text and basic v1 source metadata (filename, title, file type, page/section heading), chunk it, embed it with Amazon Titan V2, and publish versioned pgvector rows for the chat service.
- Bound vendor concurrency with a semaphore/lease budget and retry transient extraction, storage, and Bedrock failures with exponential backoff and finite attempts.
- Expose the persisted filename in owner-scoped document list/status and job status responses until deletion cleanup removes it; completed tombstones return null.
- Persist progress and results per chunk so recovery and explicit retries reuse completed work; expose counts for a future frontend's progress indicator.
- Support explicit replacement using an optional owned document ID: switch retrieval only after successful indexing, then durably remove the replaced original and derived content. Keep historical citation metadata snapshots without a file-history or rollback feature.
- Add authorized document deletion that cancels work, withdraws retrieval access immediately, and durably removes original object versions and derived chunk data.
- Emit structured logs, traces, and useful ingestion/GenAI metrics into the local stack described by `local-observability-stack`.

## Capabilities

### New Capabilities

- `document-uploads`: Authorized direct uploads, content and request deduplication, crash-safe job acceptance, progress polling, and deletion.
- `document-indexing`: Durable extraction, chunking, embedding, selective chunk retry, publication, cancellation, and source metadata for cited retrieval.

### Modified Capabilities

None.

## Impact

Creates an ingestion API and worker, MinIO integration, extensions to the shared `app` history owned by the dedicated migration service/container for upload idempotency, chunk progress, jobs, deletion, and vendor permits, and tests. Original file bytes live only in MinIO; PostgreSQL stores metadata, work state, chunk text, and vectors. It writes the index read by `horizon-agent-backend` and uses the MinIO and observability deployment in `local-observability-stack`. Frontend upload UI remains later work.

The initial chunker is a configurable deterministic Unicode-code-point sliding window with overlap, fingerprinted settings, and complete page/section provenance. The frontend consumes existing committed counts and coordinated deletion; browser integration requires exact-origin CORS on this API.
