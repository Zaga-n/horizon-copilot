## ADDED Requirements

### Requirement: Distinct provider rejection outcome
A turn whose model request the provider rejects as invalid (for example an input that exceeds the model's context) SHALL fail with a provider-rejected category that is distinct from service-unavailable. A provider outage, throttle, or access-denied response SHALL remain service-unavailable.

#### Scenario: Context window exceeded
- **WHEN** the model provider rejects a turn's request as invalid input
- **THEN** the turn ends with the provider-rejected failure category, and the stream's terminal event does not claim the service is unavailable

#### Scenario: Provider throttling
- **WHEN** the model provider throttles a turn's requests beyond the retry budget
- **THEN** the turn ends with the service-unavailable failure category

### Requirement: Every agent failure has a named outcome
Every failure raised while an agent turn runs SHALL end the turn with a named failure category:
- model calls;
- query embedding, including a malformed provider response;
- retrieval;
- guardrail classification, including an unparseable structured result;
- citation validation.

No such failure SHALL be reported as an unclassified internal error. An index-integrity fault SHALL be reported as an index-integrity failure, not as an invalid-citation failure.

#### Scenario: Malformed embedding response
- **WHEN** the embedding provider returns a successful response without a usable vector during retrieval
- **THEN** the turn ends with a named provider failure category and the run is marked failed

#### Scenario: Guardrail returns unparseable output
- **WHEN** the guardrail model's structured output cannot be parsed
- **THEN** the turn ends with a named failure category rather than an internal error
