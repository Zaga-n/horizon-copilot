## Purpose

Accept private Horizon document uploads with durable, deduplicated indexing jobs, pollable progress, and coordinated deletion for a future frontend.

## ADDED Requirements

### Requirement: Authorized direct upload
The ingestion API SHALL accept PDF or .docx content through one multipart upload request authenticated with a verified Google ID token bearer or explicitly enabled local identity. It SHALL validate size and actual file structure, compute SHA-256, store original file bytes only in private object storage, and persist metadata rather than original file bytes in PostgreSQL. It SHALL reject unsupported .doc, malformed, oversized, or mismatched files before scheduling indexing.

#### Scenario: Upload a supported document
- **WHEN** an authorized owner uploads a valid PDF or .docx
- **THEN** the API stores it and returns document/job identifiers and a status URL after durable acceptance, without waiting for extraction or embeddings

#### Scenario: Invalid file
- **WHEN** a user uploads an unsupported or invalid file
- **THEN** the API returns a clear validation error without scheduling indexing

### Requirement: Explicit document replacement
An upload MAY include an optional document_id to replace an owned live document. Without it, upload creates or deduplicates a separate document. The service SHALL retain the current searchable content until the replacement is fully indexed and atomically published. It SHALL then durably remove the replaced original and derived content while preserving content-free identifiers and saved citation metadata. No file-history or rollback API is required. At most one active replacement SHALL exist per document; competing distinct replacements or content already canonical on another document SHALL return 409. The replacement target SHALL participate in idempotency validation.

#### Scenario: Owner replaces an existing document
- **WHEN** the owner uploads corrected content with its document_id
- **THEN** the service queues indexing on that document and the previous content stays searchable until the replacement publishes

#### Scenario: Unauthorized replacement
- **WHEN** a caller selects another owner's private document as the replacement target
- **THEN** the service rejects the request without disclosing its status or content

### Requirement: Content deduplication
The service SHALL identify duplicates using a server-verified SHA-256 and indexing configuration in the authorized owner/corpus scope. Existing active or ready matches SHALL reuse their document/version/job and SHALL not start duplicate indexing, including concurrent uploads. Failed matches SHALL expose the existing failure and retry target. Matching another user's private file SHALL not disclose it. Explicitly deleted content SHALL permit a fresh upload lifecycle.

#### Scenario: Same content with another filename
- **WHEN** an owner uploads identical bytes under another filename using the same indexing configuration
- **THEN** the API returns the existing identifiers and status with deduplicated true without creating another indexing job

#### Scenario: Concurrent identical uploads
- **WHEN** two requests upload the same content into the same scope concurrently
- **THEN** both resolve to one canonical version and indexing job

#### Scenario: Indexing configuration changes
- **WHEN** the same bytes are submitted with a different configured pipeline fingerprint
- **THEN** the service permits a distinct indexing result rather than reusing incompatible chunks

### Requirement: Durable idempotent acceptance
The API SHALL accept an owner-scoped Idempotency-Key, acknowledge a new upload only after its object is stored and its metadata, queued job, and request mapping are committed, and return the same identifiers on replay. New or active jobs SHALL return HTTP 202 with a status URL; already ready or failed matches SHALL return HTTP 200 with their actual status and retry availability. A reused key with changed content or relevant metadata SHALL return HTTP 409. Accepted jobs SHALL remain executable independently of HTTP response delivery or notification delivery. Uncommitted orphan objects SHALL be recoverable or safely cleaned without deleting referenced objects.

#### Scenario: API crashes after commit before response
- **WHEN** a request commits but the API crashes before the client receives acknowledgment
- **THEN** repeating the same key and request returns the existing job identifiers without creating another indexing job

#### Scenario: Upload succeeds but DB commit fails
- **WHEN** object storage succeeds but durable acceptance does not commit
- **THEN** the API does not claim successful acceptance and subsequent retry or orphan cleanup handles the unreferenced object safely

#### Scenario: Client key reused for different input
- **WHEN** an owner uses an existing client key for different content or relevant metadata
- **THEN** the service returns a conflict instead of silently substituting another document

### Requirement: Pollable progress and completion
Authorized document list/status and job status APIs SHALL expose IDs, the persisted filename, state, stage, UTC timestamps, attempts, retry availability, and safe error categories. They SHALL expose total/completed/retrying/failed chunk counts after extraction establishes the manifest, with an unknown total beforehand. Ready SHALL mean all required chunks are committed and searchable. Failed or cancelled SHALL be terminal for the current indexing attempt, and completed progress SHALL survive retries. Other users SHALL not access private status. The required nullable `filename` field SHALL identify the job version during indexing and the document during pending deletion; completed deletion SHALL return null after metadata scrubbing. It SHALL survive navigation/reload through server responses without client filename storage.

#### Scenario: Restore document names after reload
- **WHEN** an owner reloads the document list or polls an accepted job
- **THEN** each live/deleting item includes its persisted canonical filename, including content-deduplicated uploads

#### Scenario: Deletion removes filename metadata
- **WHEN** deletion cleanup commits
- **THEN** document/job status reports deleted with filename null and the active frontend list can remove the item

#### Scenario: Progress during embedding
- **WHEN** 4 of 111 chunks have committed embeddings
- **THEN** polling returns completed_chunks 4 and total_chunks 111 promptly without waiting for remaining work

#### Scenario: Extracting before chunk count is known
- **WHEN** extraction has not established a complete manifest
- **THEN** polling identifies the extraction stage and an unknown total without inventing a percentage

#### Scenario: Completion is published
- **WHEN** all required chunks are stored and publication commits
- **THEN** polling returns ready and the frontend can stop polling and use the document in chat

#### Scenario: Another user polls
- **WHEN** a caller requests another owner's private job
- **THEN** access is denied without disclosing object references or content

### Requirement: Authorized coordinated deletion
The owner SHALL be able to delete a document through the API. Deletion acceptance SHALL immediately withdraw it from new retrieval and cancel work; durable cleanup SHALL remove its original object versions, chunk text, vectors, and detailed processing state. Deletion SHALL remain pollable as deleting until cleanup succeeds and deleted afterwards. Repeated deletion SHALL be idempotent; a failed cleanup SHALL remain recoverable by an explicit repeated DELETE that requeues the existing failed cleanup job; active/deleted replays SHALL not allocate another job. Saved answer citation metadata SHALL remain readable and identify an unavailable source without retaining deleted source content. Documents SHALL remain until explicit deletion and SHALL not expire with chat retention.

#### Scenario: Delete during indexing
- **WHEN** an owner deletes a document while embeddings are in flight
- **THEN** the API accepts deletion promptly and no stale worker can publish or restore its chunks

#### Scenario: Object storage unavailable during cleanup
- **WHEN** source-object removal temporarily fails
- **THEN** the document stays unavailable to retrieval and deleting until durable cleanup can finish

#### Scenario: Reupload after deletion
- **WHEN** an owner uploads the same bytes with a new client key after deletion completes
- **THEN** a fresh document/job can be created and stale operations from the deleted lifecycle cannot affect it
