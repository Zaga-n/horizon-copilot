## Purpose

Turn each accepted document version into a consistent, cited pgvector index while preserving progress, bounded vendor use, and safe recovery from transient failures.

## ADDED Requirements

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
