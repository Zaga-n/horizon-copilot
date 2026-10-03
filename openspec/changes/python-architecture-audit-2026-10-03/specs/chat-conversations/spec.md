## ADDED Requirements

### Requirement: Liveness reflects stopped background loops
The chat process's liveness endpoint SHALL report failure when a required background loop (recovery or retention) has stopped unexpectedly, so the platform restarts the process. While every required loop is running, liveness SHALL report success regardless of dependency outages. A loop that backs off during a dependency outage SHALL NOT count as stopped.

#### Scenario: Retention loop crashes on a defect
- **WHEN** the retention loop exits with an unexpected error
- **THEN** both readiness and liveness report failure until the process restarts

#### Scenario: Database outage during maintenance
- **WHEN** the database is unreachable and the loops back off
- **THEN** liveness keeps reporting success and the loops resume once the database recovers

### Requirement: Missing database privileges are a dependency outage
A database operation refused for insufficient privilege SHALL be classified as a dependency outage in every chat store. It SHALL NOT be classified as an unclassified internal error. API requests SHALL receive the service-unavailable problem, and background loops SHALL back off instead of stopping.

#### Scenario: Runtime grant missing after a migration
- **WHEN** a runtime role lacks a privilege that retention or recovery needs
- **THEN** the loop backs off and keeps running, and recovers without a restart once the grant is applied

### Requirement: Bounded shutdown of background maintenance
On shutdown the chat process SHALL stop its maintenance loops within a configured grace period. One retention iteration SHALL purge at most one batch, and SHALL report whether more work is due so the next batch runs promptly. Work interrupted by shutdown SHALL resume on the next start without loss.

#### Scenario: Shutdown with a large retention backlog
- **WHEN** a stop signal arrives while many conversations are due for purge
- **THEN** the process exits within the grace period, and the remaining conversations are purged after restart

#### Scenario: Backlog drains without waiting for the interval
- **WHEN** an iteration purges a full batch and more conversations are due
- **THEN** the next iteration starts without waiting for the normal retention interval
