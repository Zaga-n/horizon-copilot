# document-indexing Specification

## Purpose
Turn each accepted document version into a consistent, cited pgvector index while preserving progress, bounded vendor use, and safe recovery from transient failures.

## Requirements

### Requirement: Extract and locate source text
The pipeline SHALL extract text from supported PDF pages and `.docx` sections, preserve stable page or section locators and document version, and create bounded chunks with stable IDs. Automatic metadata extraction in v1 SHALL provide the original filename, title with filename fallback, file type, and page/section heading when available. Missing headings SHALL remain absent and Word page numbers SHALL not be invented. Basic metadata SHALL be stored separately from chunk text and available to downstream citations and future filtering; automatic project/grant/work-package/deliverable extraction is outside v1.

#### Scenario: PDF page becomes a cited chunk
- **WHEN** page 12 of an accepted PDF contains usable text
- **THEN** its published chunks retain the document version and page 12 locator for chat citations

#### Scenario: Basic metadata without a title
- **WHEN** a supported document has no clearly identifiable title
- **THEN** its filename is used as the display title and its references preserve the file type and available page/section locator

#### Scenario: Word section reference
- **WHEN** a Word chunk belongs to an extracted section heading
- **THEN** its metadata identifies that heading without fabricating a PDF-style page number

#### Scenario: No extractable text
- **WHEN** a document contains no extractable text under the supported parser policy
- **THEN** its job fails with a safe extraction category rather than publishing an empty index

### Requirement: Compatible embeddings and atomic publication
The pipeline SHALL embed chunks using the configured model and dimensions compatible with chat retrieval, and SHALL make a document version searchable only after all required chunks for that version are stored successfully. Failed or superseded versions SHALL not appear as a mixed partial index.

#### Scenario: Embedding stops midway
- **WHEN** embedding fails after some chunks have been prepared
- **THEN** chat search does not see a partial version and the job remains retryable or failed

#### Scenario: Updated document version
- **WHEN** a new version of the same document is uploaded and indexed
- **THEN** retrieval switches atomically to the new immutable version, durable cleanup removes the replaced original and chunks, and citations to older answers retain their original metadata snapshots without requiring the old file

#### Scenario: Replacement indexing fails
- **WHEN** a replacement candidate fails before publication
- **THEN** the previous published content remains searchable and the failed candidate can be retried

### Requirement: Durable bounded processing
Processing SHALL use durable jobs with leases, finite attempts, bounded backoff and jitter for retryable storage/vendor failures, and a terminal failed state for permanent errors. Vendor calls SHALL obey a configured concurrency limit across active workers so scale-out cannot exceed the intended quota.

#### Scenario: Bedrock throttles embeddings
- **WHEN** the embedding provider returns a retryable throttle response
- **THEN** the job retries after bounded backoff without exceeding the global vendor concurrency limit

#### Scenario: Worker crashes
- **WHEN** a worker exits while holding a job lease
- **THEN** another worker can recover the job after lease expiry without publishing duplicate chunks

### Requirement: Controlled reprocessing
An authorized user SHALL be able to request a new bounded retry cycle for a failed document version until deletion. Reprocessing SHALL preserve its object/version identity, persisted chunk manifest, successful chunk embeddings, and cumulative attempt history; only unfinished chunks SHALL be processed again. Automatic retry exhaustion SHALL terminate with a failed state and an explicit retry option.

#### Scenario: Retry a failed document
- **WHEN** the owner requests reprocessing after a transient failure
- **THEN** the job returns to a queued state and its status exposes the new attempt count

#### Scenario: Retry only unfinished chunks
- **WHEN** a job with 107 completed chunks out of 111 is retried
- **THEN** the four unfinished chunks are processed and the 107 committed embeddings remain unchanged

#### Scenario: Crash before embedding result is committed
- **WHEN** a provider call succeeds but the worker crashes before storing its result
- **THEN** recovery can repeat that unfinished call without duplicating chunk rows or losing other committed results

### Requirement: Recoverable job discovery
Committed jobs SHALL be discovered after worker startup or reconnect and within a configurable recovery interval even if the immediate database wakeup is missed. Future retry deadlines and expired leases SHALL become actionable without a new upload. Concurrent or expired workers SHALL not publish conflicting results.

#### Scenario: Worker was offline at acceptance
- **WHEN** an upload job commits while no worker is listening
- **THEN** a worker discovers the job after starting without requiring the user to upload again

#### Scenario: Retry becomes due without notification
- **WHEN** a scheduled retry becomes due without any new upload
- **THEN** recovery scanning discovers and processes it within the configured interval

### Requirement: Durable chunk progress and cancellation
The pipeline SHALL persist a stable complete chunk manifest before embedding and store each successful embedding with its completed state atomically. Completed results SHALL survive retries and worker restarts until explicit document deletion. A deleted or deleting document SHALL not accept further result writes or publication from stale processing attempts.

#### Scenario: Worker resumes after partial success
- **WHEN** a worker restarts after committing some chunk embeddings
- **THEN** the next valid attempt reuses those results and progress reflects committed work

#### Scenario: Delete races with publication
- **WHEN** deletion is accepted before an active attempt publishes
- **THEN** that attempt cannot make the document searchable or restore deleted chunk content

### Requirement: Ingestion telemetry
The service SHALL emit correlated structured logs and traces for upload acceptance, job execution, extraction, embedding, retry, and publication, plus bounded metrics for job status, queue age, processing duration, vendor calls, retries, and token usage when provided. Default telemetry SHALL exclude document text and credentials.

#### Scenario: Diagnose a failed job
- **WHEN** a document job reaches terminal failure
- **THEN** an operator can correlate its status and safe error category with one trace and a terminal failure log without exposing document text

### Requirement: Fixed sliding-window chunking
The initial pipeline SHALL use deterministic fixed-size sliding windows over normalized extracted text with configurable window_size and overlap measured in Unicode code points and stride window_size minus overlap. Settings SHALL satisfy window_size > 0 and 0 <= overlap < window_size. It SHALL emit no empty chunks, retain a final shorter window when needed, and stop once a window reaches the end, avoiding an overlap-only duplicate tail. Normalization, separators, chunker version, size, overlap, and units SHALL participate in the pipeline fingerprint. Each chunk SHALL preserve all source page/section locators intersecting its text interval. Retries SHALL reuse the persisted manifest rather than rechunk.

#### Scenario: Overlapping windows
- **WHEN** normalized text is 2500 code points with window_size 1000 and overlap 200
- **THEN** the manifest contains intervals [0,1000), [800,1800), and [1600,2500) in stable order

#### Scenario: Page boundary inside a window
- **WHEN** a chunk crosses a PDF page boundary
- **THEN** its metadata retains both contributing page locators for accurate downstream references

#### Scenario: Invalid overlap
- **WHEN** configured overlap is at least window_size
- **THEN** configuration validation rejects the pipeline before accepting indexing work

### Requirement: Attempt accounting survives restarts
A job's automatic attempt budget SHALL count only processing attempts that actually started work. A claim that a worker abandons on graceful shutdown or cancellation SHALL NOT consume the attempt budget. On shutdown a worker SHALL stop claiming, let running jobs settle within a configured drain grace period, and release unsettled claims for recovery.

#### Scenario: Deploys during a long job
- **WHEN** a job is interrupted by three consecutive graceful worker shutdowns before any attempt settles
- **THEN** the job is still eligible for processing and its status does not report a terminal budget failure

#### Scenario: Shutdown drains running work
- **WHEN** a worker receives a stop signal while a job is processing and the job settles within the drain grace period
- **THEN** the job's outcome is recorded before the worker exits

### Requirement: Retryable classification of transient failures
The pipeline SHALL classify a failure as retryable when it reflects a transient condition:
- a chunk deadline exceeded while waiting on the provider or the vendor permit;
- a database or object-storage outage;
- an embedding-provider throttle or 5xx;
- provider credentials that are missing, expired, or denied;
- a configured model that is not found.

Only content-caused failures SHALL be terminal without retry: no extractable text, unsupported or oversized content, and a provider rejection of the input itself. Terminal failures SHALL record a category that names the actual cause rather than a generic budget category. A 2xx provider response that cannot be parsed SHALL be recorded as a provider-protocol failure, not as a rejection of the document.

#### Scenario: Provider slowness exceeds the chunk deadline
- **WHEN** a chunk's embedding attempts exceed the chunk deadline because the provider is slow
- **THEN** the job is scheduled for a bounded retry instead of failing terminally

#### Scenario: Expired provider credentials
- **WHEN** the embedding provider rejects calls because the credentials are expired or access is denied
- **THEN** the job is retried with bounded backoff and the safe error category identifies a provider-unavailable condition

#### Scenario: Database outage during publication
- **WHEN** the database is unreachable while a job publishes its chunks
- **THEN** the job is not marked failed and becomes due again after its lease expires

#### Scenario: Stale claim conflict
- **WHEN** a stale attempt tries to complete a chunk that another attempt already owns
- **THEN** the stale attempt stops without changing the job's status and without recording a budget failure

### Requirement: Poison job isolation
A job record that cannot be read or decoded SHALL be marked failed with an integrity category and skipped. It SHALL NOT stop the worker or block later due jobs.

#### Scenario: Corrupt trace context ahead of a valid job
- **WHEN** the oldest due job carries a malformed stored trace context and a valid job is queued behind it
- **THEN** the worker keeps running, records the corrupt job as failed, and processes the valid job

### Requirement: Shared embedding construction contract
Document embeddings and chat query embeddings SHALL be produced from one shared construction contract covering:
- provider connection policy (region, timeouts, one physical attempt per call);
- credentials (optional static credentials, otherwise the default provider chain);
- model id, dimensions, and normalization.

Both services SHALL accept the same credential options.

#### Scenario: Static credentials configured for ingestion
- **WHEN** static AWS credentials are configured for the ingestion worker
- **THEN** document embeddings use those credentials exactly as chat query embeddings do

#### Scenario: Normalization stays aligned
- **WHEN** the shared embedding construction policy changes normalization or dimensions
- **THEN** both document and query embeddings change together, and chat readiness rejects index data that does not match the configured model

### Requirement: Control characters in extracted text are normalized
Before chunking, the pipeline SHALL remove NUL and every other C0 control character except tab and newline from extracted text and from the extracted title. A document SHALL NOT fail because its extracted text contains such characters. A change to this normalization SHALL change the pipeline fingerprint.

#### Scenario: PDF text contains NUL bytes
- **WHEN** a valid PDF's extracted text contains `\x00` characters
- **THEN** the document is indexed and becomes ready, and no chunk contains a control character

### Requirement: Control characters in upload metadata are rejected before storage
An upload whose filename, corpus or project metadata contains a NUL or other C0 control character (except tab and newline in free-text metadata values) SHALL be rejected with a validation problem. It SHALL be rejected before any object is written to document storage.

#### Scenario: Filename contains NUL
- **WHEN** a client uploads a file whose filename contains `\x00`
- **THEN** the response is 422 with a named validation code and no object is written to storage

### Requirement: Cleanup and delete work has a terminal exit
A cleanup or delete job that fails with a permanent failure category SHALL stop retrying after a bounded number of attempts and record a failed status. A repeated delete request for that document SHALL re-queue the work. Transient failures SHALL continue to retry with bounded backoff. Documents stuck in deletion SHALL be discoverable by a documented query.

#### Scenario: Permanent cleanup fault
- **WHEN** a cleanup job fails with a permanent category on every attempt
- **THEN** after the attempt bound the job is failed, the document stays in deletion, and a repeated delete request queues new cleanup work

#### Scenario: Storage outage during cleanup
- **WHEN** document storage is unavailable during cleanup
- **THEN** the job is retried with backoff and is not failed

### Requirement: Shutdown fits the deployment stop grace
The worker's drain grace, plus the longest in-flight provider call, plus the claim-release timeout SHALL fit within the deployment's stop grace period. The worker SHALL refuse to start when its configuration violates this bound. A chunk whose final call attempt failed SHALL be reset, so a released claim un-counts exactly the attempts that started.

#### Scenario: Provider call outlives the drain grace
- **WHEN** a stop signal arrives while an embedding call takes its full read timeout
- **THEN** the claim is released before the stop deadline, and the attempt does not count against the job's budget

### Requirement: Corrupt work records are settled consistently
A due job that cannot be decoded SHALL be marked failed with an integrity category through the same failure settlement as any other terminal failure, so its version and chunks are left in a consistent terminal state. It SHALL be logged at error with its identifier. A version, chunk or status row that cannot be decoded SHALL surface as an integrity failure, not an internal error. One corrupt document SHALL NOT fail a document listing.

#### Scenario: Corrupt job ahead of a valid job
- **WHEN** the oldest due job cannot be decoded and a valid job is queued behind it
- **THEN** the valid job is processed, and the corrupt job's document reports an integrity failure with no candidate version left processing

#### Scenario: Listing with one corrupt document
- **WHEN** a user lists documents and one stored row cannot be decoded
- **THEN** the other documents are returned and the corrupt one is reported with an integrity status

### Requirement: Provider identifier and response-shape failures are classified
An embedding call refused because the configured model identifier is invalid SHALL be retryable as provider-unavailable, not terminal as provider-rejected. A 2xx embedding response whose body is not an object, or whose vector contains non-numeric values, SHALL be recorded as a provider-protocol failure, not an internal error.

#### Scenario: Model identifier invalid in the region
- **WHEN** the provider reports the configured embedding model identifier as invalid
- **THEN** the job is retried with backoff and its category identifies provider-unavailable

#### Scenario: Non-object response body
- **WHEN** the provider returns a 2xx response whose JSON body is a list
- **THEN** the chunk attempt is recorded as a provider-protocol failure
