## ADDED Requirements

### Requirement: Invalid text input is rejected, not failed
Chat SHALL reject user text that contains NUL characters (message content, feedback comments) with a validation error before any state change. Database data errors and connection-pool exhaustion SHALL surface as a rejected request or as a service-unavailable response respectively, never as an unclassified internal error.

#### Scenario: Message containing a NUL character
- **WHEN** a user submits a turn whose content contains a NUL character
- **THEN** the API responds with a validation error and no turn, run, or message is created

#### Scenario: Connection pool exhausted
- **WHEN** no database connection becomes available within the pool timeout
- **THEN** the request receives a service-unavailable response and readiness reports unavailable rather than an internal error

### Requirement: Per-item isolation in background maintenance
Retention purges and failure recovery SHALL record a failure on the affected conversation or run and continue with the remaining items. Only a dependency outage SHALL stop a pass. An item that keeps failing SHALL NOT block other items or later turns on unrelated conversations, and SHALL be discoverable by operators through a log event and bounded metric.

#### Scenario: One conversation's checkpoint cannot be deleted
- **WHEN** a retention pass encounters a conversation whose checkpoint deletion fails with an integrity error
- **THEN** the pass purges the other due conversations, the failing conversation is recorded as degraded, and it does not stay first in every later pass

#### Scenario: Recovery intent fails for one run
- **WHEN** failure recovery cannot apply a pending failure to one run because of an integrity error
- **THEN** recovery continues for other runs, and new turns on other conversations are admitted normally

### Requirement: Supervised background loops
The recovery and retention loops SHALL run under a process supervisor that owns their cadence, failure policy, and shutdown. Readiness SHALL report not-ready when a required loop has stopped unexpectedly.

#### Scenario: Retention loop crashes
- **WHEN** the retention loop exits with an unexpected error
- **THEN** the supervisor logs the failure and readiness reports not-ready until the process is restarted
