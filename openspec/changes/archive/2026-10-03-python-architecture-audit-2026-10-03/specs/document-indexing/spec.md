## ADDED Requirements

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
