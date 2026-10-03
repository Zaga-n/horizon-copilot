## ADDED Requirements

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
