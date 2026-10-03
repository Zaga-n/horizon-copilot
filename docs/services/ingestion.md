# Ingestion service

`services/ingestion` turns uploaded PDF and Word files into searchable chunks. It is **one image
run as two independently operated processes**: an HTTP API that accepts and reports on uploads, and
an always-on worker that extracts, chunks, embeds and publishes them. Both share one PostgreSQL
database and one private, versioned MinIO bucket.

**Responsibilities:** accept uploads idempotently; store originals; extract text in isolated
subprocesses; chunk and embed with Amazon Titan; publish atomically; delete documents and clean up
exact object versions; report status; reconcile orphaned objects.
**Not responsible for:** answering questions (see [chat](chat.md)), OCR, legacy `.doc` files,
automatic project/grant metadata extraction, file history or rollback.

Package: [`services/ingestion/src/horizon_ingestion`](../../services/ingestion/src/horizon_ingestion).
Image: [`services/ingestion/Dockerfile`](../../services/ingestion/Dockerfile). A supplementary,
component-local README with a tested operator query is at
[`services/ingestion/README.md`](../../services/ingestion/README.md).

## Processes and entrypoints

`python -m horizon_ingestion.main [api|worker|worker-health]` (default `api`).

| Process | Command | Notes |
|---|---|---|
| API | `python -m horizon_ingestion.main api` | Single uvicorn process on `INGESTION_BIND_HOST:INGESTION_BIND_PORT` (8081 in Compose). Image `CMD` |
| Worker | `python -m horizon_ingestion.main worker` | Compose service `ingestion-worker`, `stop_grace_period: 75s` |
| Worker health probe | `python -m horizon_ingestion.main worker-health` | Exits 0/1 without loading settings; used as the worker's container health check |

Settings use the `INGESTION_` prefix ([configuration](../reference/configuration.md#ingestion-service)).
Both processes validate the same settings; invalid combinations fail at start-up.

### Worker lifecycle

1. Install SIGTERM/SIGINT handlers; open telemetry and the database pool.
2. **Check readiness** (the application schema revision must match); otherwise exit with
   "Ingestion requires the current application migration". The API process does not do this check
   at start-up.
3. Open the MinIO and Bedrock clients.
4. Start the `job-listener` loop and wait up to `readiness_timeout_seconds` for its first successful `LISTEN ingestion_jobs`; if that does not happen the worker exits with an error. This guarantees the worker subscribes **before** its first scan. The listener uses the database DSN directly, so a transaction-pooling proxy in front of PostgreSQL would break it. After start-up, losing the connection is tolerated and retried.
5. Start the `job-dispatch` and `orphan-reconciliation` loops (both required) and the health snapshot
   publisher.

**Shutdown.** The loops stop after their current iteration; running jobs get
`shutdown_grace_seconds` (30 s) to finish and are then cancelled. A cancelled job releases its
lease (5 s bound), un-counts the attempt and returns the in-flight chunk to `pending`, so another
worker (or the same one after restart) resumes it. Start-up validation requires the drain budget
(`shutdown_grace + provider connect + provider read + claim release` = 70 s) to fit inside
`stop_grace_seconds` (75 s), which must mirror the Compose `stop_grace_period`. If a required loop
crashes, the process exits non-zero.

### Worker health

The worker writes an atomic snapshot to `/tmp/horizon-ingestion-worker-health.json` every
`scan_interval_seconds` and removes it at shutdown. The probe fails (exit 1) when the file is
missing or invalid, a required loop crashed, no `job-dispatch` iteration has completed, the last completed iteration is older than 27 s with default settings (`scan_interval + retry_max_backoff + database connect timeout + statement timeout`), or the snapshot file itself is older than that limit (the publisher stalled). A disconnected LISTEN connection is **reported but
does not fail** the probe while scans keep succeeding; an orphan-reconciliation database outage
does not fail it either. Run it by hand:

```zsh
docker compose exec ingestion-worker python -m horizon_ingestion.main worker-health
```

## Upload and indexing flow

[Request lifecycle](../architecture/request-lifecycle.md#document-upload-to-searchable) shows the
whole path; this section states the ingestion rules.

### Acceptance (API)

`POST /v1/documents` ([contract](../reference/api.md#ingestion-api)) runs these steps in
[`application/upload_document.py`](../../services/ingestion/src/horizon_ingestion/application/upload_document.py):

1. **Validate and prepare.** Check filename, extension, exact content type, size (≤ 50 MiB), non-empty
   file, and a structural parse in an isolated child process (no text extracted yet). Authentication
   is checked before the body is parsed. An overall `upload_timeout_seconds` (120 s) bounds the whole
   request (408 on expiry).
2. **Look for an existing result.** In one transaction, upsert the owner row, take a per-owner
   advisory lock, and:
   - a stored request with the same `Idempotency-Key` and the same *request fingerprint* (content
     SHA-256 plus `document_id`, `corpus`, `project_metadata`) **replays** that job
     (`deduplicated=true`); a different fingerprint is a 409 conflict;
   - otherwise identical live content for the same owner, corpus and pipeline fingerprint resolves
     to the existing job without storing anything again; if it belongs to a different document than
     the requested replacement target, 409.
3. **Store the original** in MinIO at `attempts/<uuid>.<pdf|docx>`. The bucket must be versioned: a
   response without a version ID is rejected.
4. **Commit** (one transaction): re-check for a racing winner, take an object lock, refuse (503) if the stored object is already older than `upload_timeout_seconds` (measured from its creation, on the database clock) or is missing, then insert the document
   (if new), a `candidate` version, an `index` job (`queued`, stage `extraction`) and the idempotency
   mapping, and send `NOTIFY ingestion_jobs`.
5. Respond **202** when the job is queued, processing or retrying, or **200** when the replayed job is already ready, failed or cancelled; both carry a `Location` header and a `status_url` (the job URL).

Consequences worth knowing:

- The fingerprint excludes filename and content type. The same key with the same bytes but a different filename is a replay. A *different key* with identical live content resolves to the existing version whatever its state (even still processing or failed, in which case you get that failed job with `retry_available`); a different `project_metadata` is ignored, a different `corpus` creates a separate document, and a `document_id` naming another document is a 409.
- Idempotency mappings never expire.
- A crash or failure between storing the object and committing leaves an object under `attempts/`;
  see orphan reconciliation below. Failed candidate versions and failed `superseded_cleanup` jobs
  are not cleaned automatically (see [Known limitations](#known-limitations)).
- There is no per-user quota or rate limit in the application.

### Replacement

Uploading with an existing `document_id` (form field, or inside `metadata`, not both) targets an
owned, live document with no active index job. The previously published version stays searchable
until the replacement publishes; publication is one fenced transaction that supersedes the old
version, publishes the new one, queues a `superseded_cleanup` job for the old object and chunks, and
copies filename/title/type/metadata to the document. Saved chat citations keep their metadata but show as unavailable once their version is retired. After the replaced version is cleaned up, its original index job reads `cancelled`, so replaying the original upload's key returns 200 `cancelled`. There is no file history or rollback.

### Worker processing

- **Claim.** Every `scan_interval_seconds` (2 s), or immediately on a `NOTIFY`, the dispatcher
  claims up to `worker_concurrency − running` due jobs of any kind with
  `FOR UPDATE SKIP LOCKED` ordered by `next_retry_at`. A claim sets `processing`, increments
  `generation`, records `lease_owner` and a `lease_until` of `job_lease_seconds` (90 s). NOTIFY is
  only a wake-up hint: due retries and expired leases are found by the periodic scan.
- **Fencing.** A heartbeat extends the lease every 20 s. Every result write locks the document row
  and requires the job's generation, lease owner, unexpired lease and `processing` status (and the
  expected document lifecycle). A stale worker gets `StaleClaimError`, stops, and logs
  `ingestion_claim_fenced`.
- **Extraction.** A fresh `spawn` subprocess per parse with a wall-time limit
  (`extraction_timeout_seconds`, 60 s) and, on Linux, an address-space limit
  (`parser_memory_bytes`, 1 GiB). PDFs use pypdf (strict; encrypted files rejected; more than `max_extraction_units` = 5000 pages is rejected at upload); Word files use python-docx (paragraphs and tables; headings tracked as section headings; headers, footers and text boxes are not read). The same 5000 limit counts Word body blocks (every paragraph, empty ones included, and every table); a Word file over it passes the upload check and fails in the worker as a terminal `extraction_failed`. A Word archive whose entries expand beyond 4 × `max_extracted_chars` is rejected at upload as `malformed_file`.
  Text over `max_extracted_chars` (10,000,000) is rejected. Corrupt or oversize input is a terminal
  `extraction_failed`.
- **Chunking.** Line endings are normalised to LF, C0 control characters other than tab and LF are
  removed, units are joined with LF, then fixed windows of `window_size` 2000 code points with
  `overlap` 200 are cut. Whitespace-only windows are skipped. Each chunk keeps **every** page/section
  interval it intersects. An empty manifest or no extractable text (for example an image-only PDF)
  fails terminally as `no_extractable_text`. The whole manifest is persisted once; a re-run skips
  extraction if the manifest is complete.
- **Embedding.** One Titan text embedding v2 call per chunk (1024 dimensions, normalised), chunks
  processed **sequentially** within a document. Provider SDK retries are disabled. Concurrency is
  bounded by a per-process semaphore and by leased rows in `app.vendor_permits` (`vendor_concurrency`
  4 across all worker processes; permit lease 45 s). A permit is held only around one physical call,
  so waiting and backoff hold none.
- **Publication.** Requires a complete manifest with every chunk completed and the stored count
  equal to the manifest total; otherwise `manifest_incomplete`. Retrieval only sees a version after
  `documents.published_version_id` points to it, so partially embedded candidates are never searchable.

### Retry budgets

| Setting | Default | Meaning |
|---|---|---|
| `max_call_attempts` | 3 | Physical embedding calls per chunk in one job attempt |
| `max_chunk_attempts` | 9 | Physical calls per chunk in one retry cycle |
| `max_job_attempts` | 3 | Job attempts per retry cycle |
| `chunk_timeout_seconds` | 120 | Provider time for one chunk in one attempt (permit and backoff waits excluded). Expiry is a retryable `provider_unavailable` |
| `job_timeout_seconds` | 3600 | Wall-clock for one whole job attempt: download, extraction, embedding, publication (permit and backoff waits excluded); it also bounds cleanup jobs. Expiry fails the job terminally as `budget_exhausted`; resume it with `:retry` |
| `retry_initial_backoff_seconds` / `retry_max_backoff_seconds` | 0.5 / 10 | Backoff `min(10, 0.5·2^(n−1))` drawn from the upper half of the range, raised to any provider `retry-after` hint but never above the cap |

Failure categories decide what happens next:

| Category | Behaviour for an index job |
|---|---|
| `provider_rejected`, `extraction_failed`, `no_extractable_text`, `integrity_failed`, `budget_exhausted` | Terminal at once → `failed` |
| `storage_unavailable` (also database outages), `provider_unavailable`, `provider_protocol`, `manifest_incomplete`, `internal` | Automatic retry up to `max_job_attempts`, then `failed` with the real cause |

`budget_exhausted` has three causes: `job_timeout_seconds` expiring, a chunk reaching `max_chunk_attempts`, and a fourth attempt within one retry cycle (counting attempts that killed their worker, not ones released at shutdown). The last one stops a job that repeatedly kills its worker from looping forever. A failed job is retried by
the user with `POST /v1/ingestion-jobs/{id}:retry`: the job is re-queued in place, completed vectors
are kept, and only unfinished chunks are embedded again. `retry_available` is true only for a
`failed` index job of a live, unretired version.

### Deletion

`DELETE /v1/documents/{id}` is one transaction: the document becomes `deleting` and unpublished
(retrieval stops immediately, and chat also filters on `lifecycle='live'`); every other job of the
document is cancelled and fenced; all versions are retired; a `delete` job is queued. The worker then
removes each exact `(object_key, object_version_id)` with a versioned delete, deletes the chunks, and
tombstones the rows: object references, filename, title, corpus and metadata are cleared and the
document becomes `deleted` (the row itself is kept).

- Cleanup retries every storage and database failure indefinitely with backoff: the object-store client maps **all** S3 errors, including an access denial on removal, to `storage_unavailable`, which is never terminal. A refused removal therefore leaves the job `retrying` (look for `ingestion_job_retry` logs), the document in `deleting` and out of retrieval, and it completes by itself once the cause is fixed. A cleanup job becomes `failed` only for a permanent category after `max_job_attempts`: `integrity_failed` (for example a database integrity error) or `budget_exhausted` (for example `job_timeout_seconds` expiring); the document then stays `deleting`. **Repeating `DELETE` restarts a failed cleanup job** (it never creates a second one). The operator query for these is in
  [`services/ingestion/README.md`](../../services/ingestion/README.md) (`-- stuck-deletions`; it is
  executed by an integration test, so it is the canonical copy).
- Repeating `DELETE` on a `deleted` document returns 200.
- Re-uploading the same bytes after deletion creates a new document; the same idempotency key
  replays the old, cancelled job.

### Orphan reconciliation

The `orphan-reconciliation` loop runs every `reconciliation_interval_seconds` (60 s). Each pass inspects the next page of up to `maintenance_batch_size` (100) object versions under `attempts/`, continuing from an in-memory cursor (a full sweep takes about versions ÷ 100 minutes, and each worker sweeps independently), and removes an exact version only if no `document_versions` row references it **and** it is older than `orphan_grace_seconds` (600 s, a minimum age rather than a deadline).
Both checks run under the same per-object advisory lock that upload commit takes, so a late commit
cannot attach an object the reconciler is deleting. `orphan_grace_seconds` must exceed
`upload_timeout_seconds` (validated at start-up).

## State owned

Writes `documents`, `document_versions`, `document_chunks`, `ingestion_jobs`, `upload_requests`,
`vendor_permits` and creates `users` rows lazily; stores originals in the `horizon-documents` bucket.
It cannot read chat tables or the checkpoint schema. See [Data model](../reference/data-model.md).
The `NOTIFY`/`LISTEN` channel is documented in [Events](../reference/events.md#ingestion-job-queue).

## Ownership and authorization

The owner is the verified Google `sub` (or the fixed local subject in local identity mode). All
document, job and replacement-target lookups are owner-scoped; another owner's resources return
**404 `not_found`**, never 403, and the list returns only the caller's documents. Identical bytes
uploaded by two owners create separate documents.

## Readiness

`/health` always answers alive. `/ready` checks that `app.alembic_version` equals the schema revision
the image was built with and that the configured embedding dimension is 1024. It does **not** check
MinIO, Bedrock, the worker, the pgvector extension or index content.

## Configuration essentials

`INGESTION_ENVIRONMENT_NAME`, `INGESTION_BIND_HOST`, `INGESTION_BIND_PORT`,
`INGESTION_FRONTEND_ORIGIN`, `INGESTION_GOOGLE_CLIENT_ID`, `INGESTION_AWS_REGION`,
`INGESTION_EMBEDDING_MODEL_ID`, `INGESTION_MINIO_ENDPOINT`, `INGESTION_MINIO_BUCKET`,
`INGESTION_DATABASE_DSN`, `INGESTION_MINIO_ACCESS_KEY`, `INGESTION_MINIO_SECRET_KEY`. Policy defaults are in [`config/base.yaml`](../../config/base.yaml) (database and provider timeouts, logging) and [`config/services/ingestion.yaml`](../../config/services/ingestion.yaml) (everything else). Full inventory:
[Configuration reference](../reference/configuration.md#ingestion-service).

Provision storage with [`dev/stack/minio/provision.sh`](../../dev/stack/minio/provision.sh): it creates
the versioned, non-public bucket and a restricted account whose policy
([`ingestion-policy.json`](../../dev/stack/minio/ingestion-policy.json)) allows object operations only
under `horizon-documents/attempts/*` plus the bucket-level list/location/versioning reads. Changing
the bucket or `object_prefix` also requires changing that policy.

## Development and testing

```zsh
uv run --locked pytest services/ingestion/tests/unit
```

Integration tests run the real FastAPI app in-process against real PostgreSQL, real MinIO and the real
parsers, faking the embedding provider and using local identity (they drive the job loop directly, so worker start-up, signals, the LISTEN subscription and health publishing are not exercised); they need disposable infrastructure ([Testing](../guides/testing.md)).
Change recipes: [Development guide](../guides/development.md#change-recipes).

## Deployment and scaling

- One image, two processes. Scale the worker with replicas: claims use `SKIP LOCKED` and the
  generation fence, and `vendor_concurrency` is enforced across processes through database permits.
  `INGESTION_SERVICE_INSTANCE_ID` is the worker's lease owner; leave it unset to get a random ID per process. Compose sets a fixed name, so scaling the Compose worker service would share it (the generation fence still applies to every claim).
- Apply migrations and runtime grants before starting either process ([Deployment](../guides/deployment.md)).
- The database login must act as the `ingestion_runtime` role; the code never issues `SET ROLE`.

## Known limitations

- A failed `superseded_cleanup` job cannot be retried through the API (`:retry` returns 409 for
  non-index jobs); it leaves the replaced original and its rows until fixed by an operator.
- A failed candidate version is not retired until its document is deleted.
- `upload_requests` rows are never deleted.
- Documents are always private to the uploader; no code sets `visibility='shared'`.
- A document at the 10,000,000-character cap yields roughly 5,500 chunks embedded one at a time under
  a 3600 s job budget.

## Implementation references

[`application/upload_document.py`](../../services/ingestion/src/horizon_ingestion/application/upload_document.py),
[`application/process_job.py`](../../services/ingestion/src/horizon_ingestion/application/process_job.py),
[`application/index_version.py`](../../services/ingestion/src/horizon_ingestion/application/index_version.py),
[`db/queue.py`](../../services/ingestion/src/horizon_ingestion/db/queue.py),
[`db/fencing.py`](../../services/ingestion/src/horizon_ingestion/db/fencing.py),
[`db/status.py`](../../services/ingestion/src/horizon_ingestion/db/status.py),
[`domain/chunking.py`](../../services/ingestion/src/horizon_ingestion/domain/chunking.py),
[`workers/jobs.py`](../../services/ingestion/src/horizon_ingestion/workers/jobs.py).
