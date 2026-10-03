## Purpose

Let owners upload and manage source documents with truthful ingestion progress, recoverable processing, and visible deletion completion.

## ADDED Requirements

### Requirement: Discoverable document upload
The document panel SHALL accept PDF/.docx selection or drag-and-drop, show transfer/acceptance separately from indexing, report validation errors, and retain the upload idempotency key across transport retries for the same request. Server validation SHALL remain authoritative. An accepted upload SHALL remain visible after navigation or reload.

#### Scenario: Upload a document
- **WHEN** a valid document is accepted asynchronously
- **THEN** its document/job identifiers are retained and the panel follows server status without waiting for ingestion in the upload request

### Requirement: Stage-aware ingestion progress
The frontend SHALL show a spinner and current stage during active processing. Before the manifest exists it SHALL show an unknown total without a fabricated percentage. After it exists it SHALL show committed completed_chunks/total_chunks and a progress bar, plus retry/failure information when relevant. It SHALL show Ready only after publication, preserve counts across retries, resume owner-scoped status polling after reload, and stop indexing polling at terminal states.

#### Scenario: Extraction total unknown
- **WHEN** extraction is running and total_chunks is null
- **THEN** the panel shows an extraction spinner without a numeric fraction or invented total

#### Scenario: Three committed chunks
- **WHEN** status reports completed_chunks 3 and total_chunks 155
- **THEN** the panel shows 3/155 with active processing status

#### Scenario: All embeddings complete before publication
- **WHEN** 155/155 chunks are complete but publication is pending
- **THEN** the panel keeps a publishing status and does not report Ready

### Requirement: Failed-ingestion retry
For retryable failed jobs the frontend SHALL expose an explicit Retry action targeting the existing job and show retained completed progress. Permanent failures SHALL provide a safe explanation without advertising unavailable retry.

#### Scenario: Resume unfinished chunks
- **WHEN** a retryable job at 107/111 is retried
- **THEN** the existing document stays listed and its progress resumes from 107/111

### Requirement: Coordinated document deletion
The owner SHALL be able to delete an ingested document and also cancel/delete a processing document through the same lifecycle. The UI SHALL confirm the selected document, show Deleting after acceptance, and poll until Deleted before removing the active list entry. Cleanup failures SHALL remain visible and recoverable; accepted deletion SHALL not appear Ready or permit ingestion retry.

#### Scenario: Delete an ingested document
- **WHEN** the owner confirms deletion of a Ready document
- **THEN** the panel shows Deleting until cleanup completes and then removes the document from the active list

#### Scenario: Storage cleanup is delayed
- **WHEN** deletion is accepted but object removal is temporarily unavailable
- **THEN** the panel retains Deleting with a safe status and continues reconciliation rather than claiming complete removal
