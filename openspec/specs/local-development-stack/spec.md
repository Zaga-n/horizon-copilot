# local-development-stack Specification

## Purpose
Let a developer start and inspect the chat and ingestion services with their PostgreSQL, object storage, and telemetry dependencies using a repeatable local deployment.

## Requirements

### Requirement: Ordered local startup
The local deployment SHALL start PostgreSQL with pgvector and MinIO, run one dedicated migration service container that applies the shared `app` migration history for chat and ingestion and then initializes or migrates the LangGraph checkpoint schema, and only start the runtime services after that container exits successfully. Re-running setup SHALL be safe and SHALL not run on every service replica startup.

#### Scenario: Fresh local database
- **WHEN** a developer starts an empty local deployment
- **THEN** the application and checkpoint schemas exist before chat or ingestion accepts traffic

#### Scenario: Checkpoint setup fails
- **WHEN** checkpoint setup cannot finish
- **THEN** chat remains unready and the deployment exposes the job failure

### Requirement: Schema ownership and access
Application migrations SHALL manage only the `app` schema and SHALL ignore the `langgraph` schema. LangGraph setup SHALL manage only its own schema. Chat and ingestion runtime credentials SHALL each have only the permissions required for their own tables and operations; migration privileges SHALL be reserved for one-shot jobs.

#### Scenario: Alembic autogeneration
- **WHEN** application schema drift is checked or a revision is generated
- **THEN** LangGraph checkpoint tables do not appear as application migration changes

### Requirement: Reproducible configuration
The local deployment SHALL provide documented non-secret defaults, a secret template, persistent development volumes, explicit health checks, and a clean-start and restart procedure. It SHALL not require a frontend to start.

#### Scenario: Restart without ingestion
- **WHEN** a developer restarts the stack with an empty chunk index
- **THEN** both services start, chat can answer general in-scope questions, and it reports missing project-specific evidence clearly

### Requirement: API-mediated upload and durable worker deployment
The local stack SHALL provision a private versioned document bucket for uploads through the ingestion API and deploy an always-on ingestion worker consuming committed PostgreSQL jobs. It SHALL support database wakeup notifications, dedicated listener connections, periodic recovery, and restart-safe processing without requiring MinIO events. Original files SHALL remain in object storage and the API SHALL expose pollable job/chunk progress.

#### Scenario: Document uploaded locally
- **WHEN** a PDF or Word file upload is accepted through the ingestion API
- **THEN** the original exists in MinIO, the job is durable, and status can be polled while indexing runs independently

#### Scenario: Worker misses wakeup
- **WHEN** a committed job's wakeup is missed or the listener connection restarts
- **THEN** startup or periodic recovery discovers the job without reuploading the file

### Requirement: Authentication and maintenance configuration
The deployment SHALL document the Google ID-token bearer contract and public OAuth client audience configuration for both APIs without requiring a developer API key. It SHALL configure daily recoverable chat cleanup with a default 30-day inactivity period, lease coordination across replicas, and preservation of users, source objects, documents, chunks, and ingestion state.

#### Scenario: Daily maintenance after restart
- **WHEN** the chat service restarts after missing a scheduled retention run
- **THEN** maintenance catches up and removes expired inactive chat history and checkpoint state without expiring uploaded documents
