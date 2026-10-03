## Context

Chat reads published pgvector chunks with immutable source locations and per-user visibility. The local stack supplies MinIO, PostgreSQL, Bedrock credentials, and telemetry receivers. No ingestion implementation exists yet. See the proposal and specs for scope and observable behavior.

## Goals / Non-Goals

**Goals:** durable upload acceptance, content deduplication, visible progress, reuse of completed chunks, bounded vendor use, and coordinated deletion.

**Non-Goals:** frontend UI, OCR in the first release, legacy .doc conversion, public sharing workflows, a separate queue broker, MinIO event handling, a second Alembic history, a browsable file-version history, or rollback to replaced files.

## Decisions

### Service and storage boundaries

Deploy a separate async FastAPI ingestion API and an always-on async worker as two processes from the same ingestion package. Share the app model package with chat and extend the single Alembic history owned by the dedicated `services/migrations` container; keep ingestion actions, adapters, and lifecycle separate. Original PDF/Word bytes live only in MinIO. PostgreSQL stores document/version metadata, object references, SHA-256, pipeline fingerprint, jobs, upload idempotency mappings, chunk text/locators/vectors, retry state, and vendor permits. A document row does not contain the original file. Keeping chunk text with vectors serves retrieval and selective retries without reparsing a file for every request.

### One upload endpoint and durable acceptance

POST /v1/documents accepts multipart content, optional project metadata, an optional document_id identifying an owned live document to replace, and a required client Idempotency-Key. Omitting document_id creates a document or reuses an owner-scoped content match. Supplying it queues a replacement candidate on that document; reject unauthorized/non-live targets and content already canonical on a different document rather than merging document identities. Permit at most one active replacement per document; replay or content-match requests reuse it, conflicting replacement requests return 409. Include document_id in the request fingerprint. Verify Authorization: Bearer <Google ID token> using the same signature, issuer, configured OAuth client audience, expiry, and stable sub checks as chat. No custom developer API key is used; the public OAuth client ID is configuration. A Google API access token is not the accepted identity credential. Local test identity remains explicit and loopback-only.

Receive the file into bounded temporary storage while computing SHA-256. Validate size, declared type, and actual PDF/.docx structure before uploading to MinIO; this does not run full extraction or embeddings. Reject oversized, malformed, mismatched, or unsupported .doc files. Construct a pipeline fingerprint from parser/chunker versions and settings, embedding model, and dimensions. Check verified digest and fingerprint in the authorized owner/corpus scope. Ready or active matches reuse the existing document/version/job without reindexing. Failed matches return existing status and explicit retry target. Filename changes do not bypass deduplication, and private matches in another user's scope are not disclosed.

For a new document, upload to a private immutable object key unique to that attempt. Then one short PostgreSQL transaction inserts document/version metadata, the queued job, the owner-scoped idempotency mapping and response identifiers, and a payload-free NOTIFY ingestion_jobs. After commit return HTTP 202 with document_id, version_id, job_id, status, status_url, and deduplicated. Existing completed or failed matches return HTTP 200 with their actual status; active matches return 202. Fire-and-forget starts after file storage and durable acceptance. No second kickoff HTTP endpoint or FastAPI background task owns indexing.

Each owner/key mapping includes a request fingerprint covering file digest and validated metadata. Repeating the same request returns the same identifiers even after a lost response or completed indexing; different content/metadata under that key returns conflict. Database uniqueness on live (scope, sha256, pipeline_fingerprint) resolves concurrent uploads: one canonical version/job wins and competing requests map to that winner. Their unused object uploads become cleanup candidates, not extra jobs. The application checks for duplicates before storage where possible and enforces uniqueness again at commit.

MinIO and PostgreSQL cannot commit atomically. A crash after upload but before DB commit can leave an unreferenced object; a crash after commit but before response leaves a valid job recoverable by repeating the same key. Scheduled orphan reconciliation removes only unreferenced attempt objects after a grace period longer than the allowed upload/finalization lifetime. Final attachment and orphan deletion acquire the same object-key advisory lock, recheck references and object existence under that fence, and reject expired attachment attempts. Cleanup cannot delete a committed document's object. An S3 event would not close this transaction gap.

### Worker wakeup and durable claims

Keep a dedicated listener connection subscribed to ingestion_jobs and reconnect with bounded backoff. Establish LISTEN before scanning, then scan due jobs on startup, on notifications, and on a configurable fallback timer (initial default 2 seconds). The timer discovers due retries and expired leases without new notifications. LISTEN/NOTIFY is a wakeup hint; PostgreSQL rows are the durable queue. A stopped worker needs no notification replay.

Claim bounded batches with short FOR UPDATE SKIP LOCKED transactions, lease owner/expiry, and a monotonically increasing claim generation. Job kinds distinguish indexing from document cleanup so deletion remains executable after indexing is cancelled. Release row locks before extraction or provider I/O and heartbeat long work. Result, progress, and publication writes verify the generation and document lifecycle, fencing expired workers and cancellation. Multiple listeners may wake, but only valid claims execute work. Polling alone adds pickup latency; process-per-upload and a separate broker are unnecessary for this deployment.

### Extraction, manifest, and publication

One job indexes one immutable version. Extract PDF pages or .docx headings/paragraphs/tables with bounded size and execution time. Persist a complete deterministic chunk manifest before embedding, with stable (version_id, ordinal) IDs and chunk-content hashes tied to the pipeline fingerprint. PDF chunks retain page locators, Word chunks heading/section paths. Automatic v1 metadata extraction is limited to the original filename, document title, file type, and page/section heading. Use a clearly identifiable title from parser metadata or a document heading, otherwise the filename; missing sections stay absent. Word files expose section headings rather than invented page numbers. These values accompany each chunk and answer reference. Preserve optional user-supplied project metadata as supplied data, but automatic project/grant/WP/deliverable inference is deferred. Store the basic fields separately from chunk text so later filtering can use them; v1 does not add agent-facing metadata filter arguments or metadata-based retrieval predicates. Image-only documents fail explicitly under the no-OCR policy.

Use LangChain AWS embeddings with amazon.titan-embed-text-v2:0 at 1024 dimensions compatible with chat. Chunk states are pending, processing, retrying, completed, and failed, with attempt counters, next retry time, UTC timestamps, and safe error categories. Commit each vector and completed state together before counting it as done. Recovery processes only unfinished chunks. Saved text and successful vectors survive failure and explicit retries until deletion. A provider response lost before result commit may require repeating that physical call; exactly-once vendor execution is not promised.

Candidate chunks stay hidden until every required chunk is completed. One fenced transaction marks the version published/ready and supersedes any previous published version when applicable. Ready means searchable, not just uploaded or embedded. Completed candidate chunks remain reusable when the overall job fails; saved citations keep their immutable version IDs and metadata snapshots. After the replacement publishes, enqueue durable cleanup of the superseded object and chunks. Do not retain old original files or chunk content as a file-history feature; retain only content-free identifiers for idempotency and stale-operation fencing. Until replacement publication, keep the previous published version searchable, including when the replacement fails.

### Initial chunking algorithm

Use a vanilla fixed sliding window over the entire normalized document text, measured in Unicode code points, initially window_size=2000 and overlap=200 (stride 1800). These are configurable initial defaults, not a tokenizer-dependent token promise. Normalize line endings to LF, preserve extracted text order, and join extraction units with a fixed newline separator while retaining a source-offset map. Start at offset 0; emit [start, min(start+size, length)), stop when end equals length, otherwise advance by stride. Empty text fails extraction. Do not use semantic or recursive splitting in v1. Retain start/end offsets and all intersecting page/section locators, with a primary locator for compatible citation displays; chat sources/retrieval also preserve the full locator collection. Include normalization/separator policy, units, algorithm version, size, and overlap in the fingerprint. Persist the deterministic manifest once before embeddings. Defaults favor simplicity; changing them creates a distinct indexing configuration instead of silently mixing chunks.

### Retry budgets and progress polling

Use an asyncio semaphore per process plus leased PostgreSQL permits for the global Bedrock concurrency cap. One bounded retry wrapper owns each physical storage/vendor attempt, with exponential backoff, jitter, and provider retry hints for transient failures. Release vendor permits while waiting for backoff. Coordinate finite call, chunk, and job-recovery budgets rather than multiplying nested retries. Permanent validation/extraction errors fail immediately. After automatic exhaustion, POST /v1/ingestion-jobs/{id}:retry starts a new bounded retry cycle for unfinished chunks only, retaining successful results and cumulative attempt history. Deleting/deleted documents cannot retry.

GET /v1/documents, GET /v1/documents/{id}, and GET /v1/ingestion-jobs/{id} enforce ownership. Return stable IDs, state, stage, timestamps, attempt information, safe error category, retry availability, and durable progress counts. Indexing states are queued, processing, retrying, ready, and failed; document lifecycle additionally has deleting/deleted and cancelled work exposes cancelled. Stages distinguish extraction, embedding, and publication. total_chunks is null until the manifest is complete; afterwards expose total_chunks, completed_chunks, retrying_chunks, and failed_chunks. Retries preserve completed progress. Clients stop indexing polling at ready, failed, or cancelled and deletion polling at deleted. Status exposes neither chunk text nor credentials nor raw exceptions.

### Deletion and citations

DELETE /v1/documents/{id} checks ownership, atomically marks deleting, withdraws retrieval visibility, cancels pending work, fences active claims, and records durable cleanup; return 202 with a pollable document ID. Work cannot publish after deletion begins. Cleanup retries removal of all original object versions owned by that document, chunk text/vectors, and detailed job/chunk state. A versioned-bucket delete marker alone is insufficient. Mark deleted only after cleanup succeeds. Retain minimal content-free tombstones for polling, idempotency replay, and stale-operation safety. Repeated DELETE returns the same lifecycle outcome.

Superseded-content cleanup shares the exact-version deletion and fencing guarantees without changing the live document lifecycle or removing the newly published content. Saved answer sources are metadata snapshots without restrictive foreign keys to removable chunks. They remain readable and can report source unavailable after deletion; old answer text is preserved. Upload after completed deletion with a new client key creates a fresh lifecycle and object identity. Delayed writes/deletes cannot affect the new lifecycle. Uploaded documents, source objects, successful chunks, and failed work are excluded from chat's 30-day retention.

### Access, observability, and deployment

Private visibility is default. SQL filters owner/shared visibility and deleting/deleted documents before ranking. Ingestion runtime can write its document/job/idempotency/chunk/permit tables and read minimal identity fields; it cannot read chat messages/checkpoints or alter schemas. MinIO credentials are bucket-scoped and never provided to a model.

Use bounded traces for uploads, attempts, deletion, and reconciliation, linking worker traces to persisted acceptance context. Emit extraction, physical embedding, and publication spans, terminal/retry logs, and bounded metrics for queue age, jobs, duration, vendor usage/retries, completed chunks, and cleanup failure. IDs/hashes are not metric labels and document content is excluded by default. Grafana and Langfuse receive one OTel emission path.

## Risks / Trade-offs

- **Lost HTTP acknowledgment** → persist client-key mappings with the job and replay identifiers.
- **Storage and DB divergence** → immutable attempts, uniqueness constraints, and fenced orphan cleanup.
- **Lost wakeup or disconnected listener** → startup/reconnect scans and periodic recovery.
- **Worker lease expires during a call** → heartbeat and fenced writes; physical calls may repeat after crashes.
- **Retries waste successful work** → persist the manifest and vector/state together and resume unfinished chunks.
- **Deletion races with indexing** → visibility withdrawal, claim fencing, and durable exact-version cleanup.
- **Parsing loses layout or needs OCR** → preserve locators and explicitly report unsupported extraction.

## Migration Plan

Extend the app history in `services/migrations` with digest/fingerprint constraints, request mappings, chunk progress, leases, deletion lifecycle, and vendor permits; exclude langgraph. Provision a private versioned MinIO bucket and runtime roles, then run the same one-shot migration container used by chat before deploying the API and listener worker. Ingestion owns no competing Alembic history and does not run migrations at startup. Verify direct PDF/.docx uploads, concurrent duplicates, lost-response replay, missed wakeup recovery, selective retries, deletion races, orphan cleanup, and cited chat retrieval. Rollback stops services without deleting originals or reusable candidates; destructive schema downgrade is manual.

## References

- [PostgreSQL NOTIFY transaction behavior](https://www.postgresql.org/docs/current/sql-notify.html)
- [PostgreSQL queue claims with SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html)
- [Google backend ID-token verification](https://developers.google.com/identity/sign-in/web/backend-auth)

### Filename in lifecycle responses

`JobStatus` includes a required nullable `filename` field. For indexing jobs,
project the existing version filename, falling back to document metadata for
legacy rows; deletion jobs use document metadata while cleanup is pending.
Owner-scoped list, document status, job status, and retry responses share this
projection. Once deletion cleanup commits, `filename` is null along with the
other scrubbed source metadata. Never retain filenames in tombstones to satisfy
the UI. Accepted upload identifiers remain unchanged; the client reads status
to obtain the canonical filename, including deduplicated uploads. This additive
response change uses existing columns and requires no migration.

An explicit repeated DELETE while its cleanup job is failed requeues that same
job with a new fenced retry cycle. Active/finished deletion remains idempotent.
This gives the frontend's existing recoverable-cleanup requirement an executable
recovery path after finite automatic retries, without creating another job or
restarting ingestion.
