## ADDED Requirements

### Requirement: Failure classes are never folded into each other
A turn failure SHALL keep the class of its cause through retrieval, tools and the model runner:
- **unavailable:** outage, throttle, access denied, unknown or unavailable model identifier;
- **rejected:** the provider rejects the input itself;
- **invalid output:** a 2xx response that cannot be parsed, including a malformed embedding body and an unparseable guardrail result.

Only the unavailable class SHALL be retried inside a turn. A misconfigured model identifier SHALL end turns as service-unavailable, not provider-rejected.

#### Scenario: Unknown model identifier
- **WHEN** the provider reports that the configured model identifier is invalid in the configured region
- **THEN** the turn ends with the service-unavailable category, not provider-rejected

#### Scenario: Embedding provider rejects the query
- **WHEN** query embedding is rejected by the provider as invalid input
- **THEN** the tool is not retried and the turn ends with the provider-rejected category

#### Scenario: Malformed model response
- **WHEN** the chat model returns a successful response whose body cannot be parsed
- **THEN** the turn ends with a named provider-protocol category, not an internal error, and is not retried

### Requirement: Retrieval failures are classified and isolated per row
A retrieval connection-pool timeout or a network error SHALL be classified as unavailable. A stored chunk row whose source or locator cannot be decoded SHALL be skipped and logged as an index-integrity fault, and the remaining evidence SHALL be used. A locator field this service does not recognise SHALL NOT make a row undecodable.

#### Scenario: Pool exhausted under load
- **WHEN** retrieval cannot obtain a database connection within its timeout
- **THEN** the search is retried as transient and, if the retry budget is exhausted, the turn ends as service-unavailable

#### Scenario: Ingestion adds a locator field
- **WHEN** indexed chunks carry a locator key this service does not know
- **THEN** retrieval returns those chunks as evidence and no turn fails as internal

### Requirement: Lost turn lease ends as interrupted
When a turn's lease has expired and another actor has taken over or failed the run, the original stream SHALL end with the interrupted category. It SHALL NOT be reported as an internal error.

#### Scenario: Slow client outlives the lease
- **WHEN** a stream's run lease expires before the answer is persisted
- **THEN** the stream ends with the interrupted category and the run's stored terminal status is unchanged by the late writer
