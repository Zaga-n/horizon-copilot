# Document ingestion

Run the API and always-on worker as separate processes against the same
PostgreSQL database and private versioned MinIO bucket. Apply the shared migration
job and runtime grants first. The database login must assume `ingestion_runtime`.
The worker needs Bedrock InvokeModel access to Titan V2, through the standard AWS
SDK credential chain or the optional `INGESTION_AWS_*` static credentials (the same
options chat accepts).

Copy required deployment values from `.env.example` into the process environment
or repository `.env`. Policy layers shared `config/base.yaml`, environment YAML
and `config/services/ingestion[.<environment>].yaml`. Prefixed environment
variables override policy. Installed deployments use `HORIZON_CONFIG_DIR`,
already configured in Docker. Local identity requires a loopback binding;
public deployments require Google ID tokens and an HTTPS frontend origin.

Provision storage with `dev/stack/minio/provision.sh`, using `mc`, privileged
`MC_HOST_local`, and `INGESTION_MINIO_ACCESS_KEY`/`INGESTION_MINIO_SECRET_KEY`.
The policy covers `horizon-documents/attempts/*`. Changing bucket or prefix also
requires changing the platform policy. Originals exist as exact object versions
in MinIO; PostgreSQL stores metadata, extracted chunks and vectors.

```sh
uv run --locked --package horizon-ingestion python -m horizon_ingestion.main api
uv run --locked --package horizon-ingestion python -m horizon_ingestion.main worker
docker build -f services/ingestion/Dockerfile -t horizon-ingestion:local .
docker run --rm --env-file .env horizon-ingestion:local python -m horizon_ingestion.main worker
```

Upload a PDF or `.docx` with the matching MIME type, bearer token and stable
idempotency key. Optional metadata accepts `corpus`, `project_metadata` and
`document_id`. An explicit document ID selects an owned live replacement target;
omitting it creates a document or resolves matching content. The published
original stays searchable until the replacement completes. Successful publication
queues removal of the superseded original and chunks. Saved chat citation
metadata remains; file history and rollback are not provided.

```sh
curl -X POST http://localhost:8081/v1/documents \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" \
  -H "Idempotency-Key: upload-example-1" \
  -F 'file=@proposal.pdf;type=application/pdf' \
  -F 'metadata={"corpus":"projects","project_metadata":{"project":"Horizon"}}'
```

A 202 acknowledgment follows object storage and the metadata/job commit. Poll
its `status_url`. Replaying a key resolves the same job; changed content or
metadata returns 409. Content deduplication is scoped to owner, corpus, digest
and pipeline fingerprint. Competing replacements return 409.

- `GET /v1/documents` lists owned work with bounded `limit`/`offset`.
- `GET /v1/documents/{id}` and `GET /v1/ingestion-jobs/{id}` expose lifecycle,
  stage, attempts, safe errors and durable chunk counts. Total is null until the
  complete manifest is persisted. The required nullable `filename` comes from
  persisted metadata: the candidate version for index jobs, or the document for
  deletion. It remains available while deleting and becomes null after cleanup;
  the additive response field requires no database migration.
- `POST /v1/ingestion-jobs/{id}:retry` resumes unfinished chunks in a failed job.
  Completed vectors remain; a ready result is not reindexed by retry.
- `DELETE /v1/documents/{id}` immediately excludes retrieval and fences active
  writes. Poll until durable exact-version cleanup reaches `deleted`. Cleanup
  survives storage outages; repeating deletion is safe. If automatic cleanup
  attempts are exhausted, repeating DELETE starts another fenced retry cycle on
  the same cleanup job without creating a duplicate.

## Operations

Cleanup retries storage and database outages with backoff indefinitely. A permanent
cleanup fault (for example, a refused object removal) stops after `max_job_attempts`
and records the job as `failed`, leaving the document in `deleting` and out of
retrieval. Find those documents, fix the cause, then repeat `DELETE` for each to
start another cleanup cycle:

```sql
-- stuck-deletions
SELECT d.id AS document_id, j.id AS job_id, j.error_category, j.cycle_attempts, j.updated_at
FROM app.documents d
JOIN app.ingestion_jobs j ON j.document_id = d.id AND j.kind = 'delete'
WHERE d.lifecycle = 'deleting' AND j.status = 'failed'
ORDER BY j.updated_at;
```

Failed `superseded_cleanup` jobs are listed by the same table with
`kind = 'superseded_cleanup'`; they leave only the replaced original and its rows.

## Pipeline

Initial windows contain 2000 Unicode code points with 200 overlap. Extraction
normalizes line endings, removes C0 control characters other than tab and newline,
joins source units with LF and preserves every intersecting page/section
interval. Uploads whose filename, corpus or metadata contain control characters
are rejected with 422 before the original is stored. Parsers run in isolated subprocesses with
output, unit and wall-time limits, plus a Linux memory limit. Legacy `.doc`,
encrypted/malformed and oversized files are rejected. Image-only PDFs finish
with `no_extractable_text`. OCR and automatic project/grant/WP extraction are
not included.

Workers subscribe before scanning, reconnect and periodically scan durable due
jobs when notifications are lost. Generation fences reject expired workers.
Embeddings use 1024 dimensions, disabled SDK transport retries, finite
call/chunk/job budgets and shared leased PostgreSQL permits. Orphan reconciliation
advances bounded object-list pages and shares the object fence with finalization.
Keep orphan grace above the acceptance deadline.

Indexing defaults to four concurrent jobs and four in-flight embedding calls,
using a shared process semaphore and a database permit cap across worker processes.
Each document processes its chunks sequentially. Throttling gets up to three
physical calls per chunk in one job attempt, with exponential backoff and jitter
from 0.5 seconds capped at 10 seconds. Backoff releases both permits; waiting for
capacity does not consume the job's work-time budget. Retry cycles resume unfinished
chunks, bounded by nine physical chunk calls and three job attempts per cycle.
Tune these limits in `config/services/ingestion.yaml` or the corresponding
`INGESTION_` process environment variables.

Set `INGESTION_OTLP_ENDPOINT` to the Collector HTTP base URL for one OTLP trace
and metric path. JSON stdout logs carry active trace IDs. Durable worker roots
link to upload traces. Metrics cover queue age, jobs, chunks, physical vendor
calls, retries, cleanup and optional input tokens. Content, credentials and raw
exception messages are excluded; metric labels exclude IDs. The local
Collector/Grafana stack belongs to a separate change.

Offline integration uses disposable PostgreSQL and MinIO. Set
`TEST_DATABASE_DSN` to an admin URL for a database ending in `_test`, and
`TEST_MINIO_ENDPOINT` to disposable MinIO. Fixtures expect the CI storage account
`test-ingestion` / `disposable-ingestion-password`, provisioned by the platform
script. Run `uv run --locked pytest -m integration`. Tests use real parsers,
PostgreSQL, MinIO, LangChain and the compiled chat agent, with controlled paid
model boundaries. Online Bedrock access and quotas are not verified.
