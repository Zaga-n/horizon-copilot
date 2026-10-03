## 1. Shared schema and service foundation

- [x] 1.1 Add ingestion API/worker entry points, typed settings, pinned dependencies, and architecture checks; verify clean installation and quality commands.
- [x] 1.2 Extend the shared app models and the single app history in `services/migrations` with metadata-only document rows, SHA-256/pipeline uniqueness, owner-scoped request mappings, chunk progress, fenced job leases, deletion lifecycle, and global vendor permits; verify fresh replay, retrieval compatibility, and no langgraph diff.
- [x] 1.3 Configure ingestion DB and private bucket-scoped MinIO roles; verify no access to chat messages/checkpoints or schema alteration.

## 2. Upload acceptance and polling

- [x] 2.1 Implement Google ID-token bearer/local identity checks and one bounded multipart POST /v1/documents with actual format validation, temporary spooling, SHA-256, and immutable MinIO upload; verify valid PDF/.docx, invalid/oversized files, and temporary-file cleanup.
- [x] 2.2 Implement content deduplication and atomic metadata/job/request-mapping acceptance with NOTIFY; verify sequential/concurrent duplicates, changed configuration, key conflicts, explicit owned replacement targets, competing-replacement conflicts, and cross-user privacy.
- [x] 2.3 Verify crashes before and after DB commit and lost HTTP acknowledgment; repeated requests must resolve to one committed job, and acceptance must never precede file storage/commit.
- [x] 2.4 Implement owner-filtered document/job polling with stage, null initial total, durable chunk counts, attempts, safe errors, and retry targets; verify 4/111 and terminal ready/failed/cancelled outcomes.
- [x] 2.5 Implement fenced, age-bounded orphan-object reconciliation; verify cleanup never removes referenced or actively finalizing uploads and expired finalization cannot attach a removed object.

## 3. Extraction and indexing

- [x] 3.1 Implement bounded PDF page and .docx section extraction with locators and explicit image-only failure; verify correct page/section provenance.
- [x] 3.2 Persist a complete deterministic manifest with stable IDs/content hashes and basic v1 metadata (filename, title with filename fallback, file type, page/section heading) before embedding; verify correct PDF/.docx references, missing-heading behavior, and manifest reuse after restart. Defer automatic project/grant/WP extraction.
- [x] 3.3 Implement Titan V2 LangChain embeddings at 1024 dimensions with per-process semaphore/global leased permits; verify two workers cannot exceed the configured vendor cap.
- [x] 3.4 Commit vectors and completed chunk states together and publish only a fully completed version; verify hidden partial candidates, accurate progress, atomic replacement with old content available until success, durable removal of superseded originals/chunks, and retained citation snapshots without file history.

## 4. Durable worker and retries

- [x] 4.1 Implement the always-on LISTEN/NOTIFY worker, dedicated reconnecting listener, scan after subscription, periodic due-job scans, and bounded claims with SKIP LOCKED/heartbeats/generation fencing; verify missed wakeups, stopped/restarted workers, due retries, and stale-worker rejection.
- [x] 4.2 Add bounded backoff/jitter retries for transient storage/vendor calls and coordinated finite call/chunk/job budgets; verify throttling, permit release during backoff, permanent failures, and terminal exhaustion.
- [x] 4.3 Resume only unfinished chunks after crash and explicit retry; verify a 107/111 completed job retries the remaining four while retaining vectors/progress and cumulative attempt history.
- [x] 4.4 Implement authorized retry endpoints and notifications; verify failed-job retry cannot duplicate a ready result or restart deleting/deleted content.

## 5. Deletion

- [x] 5.1 Implement idempotent DELETE with immediate retrieval exclusion, cancellation fencing, durable cleanup, and pollable deleting/deleted states; verify deletion during an active provider call cannot be undone by stale writes.
- [x] 5.2 Remove exact MinIO object versions, chunk content/vectors, and detailed work state, retaining minimal content-free tombstones; verify storage outage recovery, repeated delete, historical citation snapshots, and fresh reupload isolation.

## 6. Telemetry and integration

- [x] 6.1 Add correlated logs/bounded traces and queue, job, chunk, vendor, and cleanup metrics; verify IDs/hashes are not metric labels and document content/credentials are excluded.
- [x] 6.2 Run PDF/.docx upload-to-cited-chat integration tests covering polling, listener recovery, idempotency, selective retries, deletion races, and owner isolation; verify originals exist only in MinIO and chat retention leaves documents intact.

## 7. Initial chunker and frontend integration

- [x] 7.1 Add fixed-window settings (initial 2000-code-point size, 200 overlap), normalization/offset mapping, fingerprint fields, and deterministic manifest construction; verify overlap bounds, empty/short/exact-end documents, final tails, cross-page locators, and restart manifest reuse.
- [x] 7.2 Extend shared chunk/source metadata to preserve all intersecting page/section locators and text intervals; verify upload-to-chat references remain accurate for windows crossing source boundaries.
- [x] 7.3 Configure exact frontend-origin CORS for upload/status/retry/delete and required bearer/idempotency headers; verify browser preflights and access rejection for unapproved origins.

- [x] 7.4 Expose persisted nullable filename in owned list/document/job/retry status responses; verify canonical names after reload/deduplication and replacement, name retention during deletion, null after cleanup, and owner isolation without a migration.
