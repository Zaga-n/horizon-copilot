# Data model

All application state lives in one PostgreSQL database (with the `pgvector` extension) plus one
versioned MinIO bucket for original files. This page describes the tables, their ownership, the roles
that may touch them, and how data is retained and deleted.

- Table definitions: [`libs/schema/src/horizon_schema/models.py`](../../libs/schema/src/horizon_schema/models.py)
  (SQLAlchemy Core `Table` objects in schema `app`, with a constraint naming convention in
  [`metadata.py`](../../libs/schema/src/horizon_schema/metadata.py)).
- History: Alembic revisions owned by the [migration job](../services/migrations.md).
- Conventions: most primary keys are application-generated UUIDs (no database default; exceptions are the composite natural keys of the idempotency and feedback tables and `vendor_permits.slot`); most tables carry `created_at` and `updated_at` (`timestamptz NOT NULL DEFAULT now()`, with `updated_at` maintained by application code and no trigger; exceptions are `vendor_permits`, and `agent_runs`, which has `started_at` and `ended_at`); status vocabularies are `CHECK` constrained text columns whose values are defined
  by `StrEnum`s in each service's `domain/`. Both services bind only the tables they use in their own
  `db/tables.py`.

## Schemas and ownership

| Schema | Owner role | Contents | Written by |
|---|---|---|---|
| `app` | `app_migrator` | All application tables below, plus `app.alembic_version` | chat and ingestion (disjoint table groups, except `users`, which both write) |
| `langgraph` | `checkpoint_migrator` | LangGraph checkpoint tables created by `AsyncPostgresSaver.setup()` | chat only |
| `public` | | The `vector` extension; `CREATE` on it is revoked from `PUBLIC` | |

## Entity relationships

```text
users 1──* conversations 1──* turns 1──* messages           (turn: 1 user message + 0..n assistant messages)
                       │         └──* agent_runs 1──1 assistant message   (one run per attempt; retry_of_run_id chain)
                       ├──* retry_requests ──→ agent_runs
                       ├──* message_feedback ──→ assistant messages
                       └──0..1 thread_feedback (per user) ──→ agent_runs (context)
conversations.active_run_id ──→ agent_runs   (0..1 leased run per conversation)

users 1──* documents 1──* document_versions 1──* document_chunks   (published_version_id: 0..1 per document)
documents 1──* ingestion_jobs ──→ document_versions
users 1──* upload_requests ──→ documents, document_versions, ingestion_jobs
vendor_permits   (no relations: global concurrency slots)
```

Non-foreign-key links worth knowing: the conversation UUID is the LangGraph `thread_id`;
`messages.sources` (JSONB) snapshots citation metadata and deliberately has no foreign key to chunks, so
documents can be deleted while old answers stay readable; `agent_runs.base_checkpoint_id` is a logical
LangGraph checkpoint ID; `agent_runs.trace_id` is the OpenTelemetry trace ID. Composite foreign keys pin owners: `turns`, `retry_requests` and the two feedback tables carry `(conversation_id, user_id)`, and messages and runs are pinned through `(turn_id, conversation_id)`, so a child can never belong to a different owner than its conversation.

## Users

`users(id, subject UNIQUE, created_at, updated_at)`. `subject` is the verified Google `sub` (or the local
subject `local-horizon-user`); nothing else about a person is stored: no email or name. Either service
creates the row lazily (`INSERT … ON CONFLICT (subject) DO NOTHING`) on the owner's first conversation
or first upload. Rows are never updated or deleted by code, and retention does not remove them.

## Chat tables (written by `chat_runtime`)

| Table | Purpose | Key constraints |
|---|---|---|
| `conversations` | One thread per owner; also holds the active-run lease and message counter | `status` ∈ `active`, `purging`; `active_run_id` and `lease_until` are both null or both set; `next_message_order > 0`; index on `(user_id, last_activity_at)` and `(status, last_activity_at)` |
| `turns` | One user request; idempotency anchor | `UNIQUE (user_id, conversation_id, request_key)`; `request_key` 1–200 chars; `input_hash` is the SHA-256 of the content; `status` ∈ `active`, `completed`, `failed` |
| `messages` | User and assistant messages, including pending partial answers and citations | `role` ∈ `user`, `assistant`; `status` ∈ `pending`, `completed`, `failed`; user messages are attempt 0 and completed, assistant messages attempt ≥ 1; `UNIQUE (conversation_id, message_order)`; `sources` is a JSON array |
| `agent_runs` | One assistant attempt: trace identity, checkpoint bookkeeping, outcome, versions | `UNIQUE (turn_id, attempt_number)`; `UNIQUE (assistant_message_id)`; `trace_id` 32 lower-case hex and `root_span_id` 16, neither all zeros; `ended_at` null exactly while pending; indexes on `(conversation_id, started_at)` and `trace_id`; `failure_category` set exactly when failed (free text at the database level; values in the [chat page](../services/chat.md#failure-handling-and-recovery)) |
| `retry_requests` | Idempotency ledger for retry requests | PK `(user_id, conversation_id, request_key)`; references the expected and assigned runs |
| `message_feedback` | Like/dislike (+ comment on dislike) on an assistant message | PK `(user_id, assistant_message_id)`; comment only with `dislike`, 1–4000 chars |
| `thread_feedback` | One conversation-level rating and/or comment per user | PK `(user_id, conversation_id)`; needs a rating or a non-blank comment; stores `context_run_id` and `context_message_order` |

Three cyclic foreign keys are `DEFERRABLE INITIALLY DEFERRED` and added after table creation:
`conversations.active_run_id → agent_runs`, `turns.user_message_id → messages`, and
`documents.published_version_id → document_versions`.

## Document tables (written by `ingestion_runtime`, read by chat)

| Table | Purpose | Key constraints |
|---|---|---|
| `documents` | The logical document; becomes a tombstone without filename, title, text or object reference after deletion (the row is never physically deleted by code) | `lifecycle` ∈ `live`, `deleting`, `deleted`; `visibility` ∈ `private`, `shared` (default `private`; no code sets `shared`); `file_type` ∈ `pdf`, `docx`; `filename`, `title`, `file_type` are nullable so tombstoning can clear them; index `ix_documents_visibility (user_id, visibility, lifecycle)` |
| `document_versions` | An upload/index attempt of a document | `status` ∈ `candidate`, `published`, `superseded`, `failed`; `embedding_dimensions = 1024`; unique partial index `(user_id, corpus, sha256, pipeline_fingerprint) WHERE retired_at IS NULL` (live-content uniqueness); `object_key` and `object_version_id` point to the MinIO object and are nulled at cleanup; `user_id` is nullable (a null owner would defeat the uniqueness index, but ingestion always sets it) |
| `document_chunks` | Extracted text with its embedding: the retrieval corpus | `UNIQUE (version_id, ordinal)`; `embedding vector(1024)`; `status` ∈ `pending`, `processing`, `retrying`, `completed`, `failed`; a completed chunk must have an embedding; non-blank filename and text; `locators` is a JSON array of `{page, section_heading, start_offset, end_offset}`; per-chunk `attempts`, `cycle_attempts`, `next_retry_at`, `error_category` |
| `ingestion_jobs` | The durable work queue | `kind` ∈ `index`, `delete`, `superseded_cleanup`; `status` ∈ `queued`, `processing`, `retrying`, `ready`, `failed`, `cancelled`; `stage` ∈ `extraction`, `embedding`, `publication`, `cleanup`, `done`; lease columns paired; `generation` fence; `trace_context` JSONB. Partial unique indexes: one index job per version, one *active* index job per document, one delete job per document; partial index on due work |
| `upload_requests` | Owner-scoped upload idempotency ledger | PK `(user_id, request_key)`; stores the request fingerprint and the resulting document, version and job |
| `vendor_permits` | Global leased slots that cap concurrent embedding calls across worker processes | PK `slot`; `token` and `lease_until` paired; slots `1..vendor_concurrency` are created lazily |

**Vector index.** `ix_document_chunks_embedding` is `USING hnsw (embedding vector_cosine_ops)` with
pgvector's default build parameters; the chat query orders by cosine distance (`<=>`), matching the
operator class. No `ef_search` or iterative-scan setting is configured, and whether the planner uses
the index for the filtered, joined retrieval query has not been verified.

### Document vocabularies

`error_category` (jobs and chunks, free text at the database level): `storage_unavailable`,
`provider_unavailable`, `provider_protocol`, `provider_rejected`, `extraction_failed`,
`no_extractable_text`, `manifest_incomplete`, `integrity_failed`, `budget_exhausted`, `internal`.

## LangGraph checkpoint schema

Created by LangGraph's own migrations (`checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes`, pinned through `langgraph-checkpoint-postgres 3.1.2`), not by Alembic. Chat
readiness requires the highest applied checkpoint migration to equal the library's expected version.
There is no foreign key into `app`; the link is `thread_id = conversations.id`. Chat reads checkpoints
and deletes a thread when a conversation is purged, and **never runs `setup()`**.

## Roles and privileges

Defined by [`services/migrations/sql/provision.sql`](../../services/migrations/sql/provision.sql) and
[`runtime_grants.sql`](../../services/migrations/sql/runtime_grants.sql). The four roles are `NOLOGIN`
in production provisioning; the platform supplies authenticated logins that act as them. The code
itself never issues `SET ROLE`.

| Role | Schema rights | Table rights |
|---|---|---|
| `app_migrator` | Owns `app`; no rights on `langgraph` | DDL on `app` |
| `checkpoint_migrator` | Owns `langgraph`; no rights on `app`; may execute `public.horizon_setup_checkpoint_schema()` | DDL on `langgraph` |
| `chat_runtime` | `USAGE` on `app` and `langgraph`; no `CREATE` anywhere | `users`: select, insert, update; chat tables: select, insert, update, delete; `documents`, `document_versions`, `document_chunks`, `alembic_version`: **select only**; `langgraph.*`: select, insert, update, delete (also by default privileges); nothing on `ingestion_jobs`, `upload_requests`, `vendor_permits` |
| `ingestion_runtime` | `USAGE` on `app` only; no `langgraph` access; no `CREATE` | `users`: select `(id, subject)` and insert; document, job, request and permit tables: select, insert, update, delete; `alembic_version`: select; **no access to chat tables** |

Both runtime roles have `search_path = app, public`, `statement_timeout = 10s` and `lock_timeout = 5s`
set at role level by `runtime_grants.sql`. `ALTER DEFAULT PRIVILEGES … REVOKE ALL … FROM PUBLIC` means
new `app` tables get **no** automatic grants: every new table needs explicit lines in
`runtime_grants.sql`.

Caveats:

- PostgreSQL applies `ALTER ROLE … SET` defaults only to sessions that *log in as* the role, not to
  sessions that assume it. In a `NOLOGIN` + assumption deployment the role-level `search_path`,
  `statement_timeout` and `lock_timeout` do not apply unless the platform sets them on the login
  identity. The services compensate partly (both pass `statement_timeout` as a connection option,
  ingestion sets `lock_timeout` per transaction, and SQL is schema-qualified), but chat's 5 s
  `lock_timeout` is at risk. How production logins assume roles is not determined from the repository.
- `runtime_grants.sql` grants somewhat more than the code was seen to use (for example chat `UPDATE` on
  `users`, chat `DELETE` on turn/message/run tables, ingestion `DELETE` on tables where only chunks are
  deleted). Removing them was not tested.
- Re-creating the `langgraph` schema through the helper function (as the migration job does when it is
  missing) would lose the `USAGE` grant and default privileges that only `provision.sql` sets; re-run `provision.sql` and then `runtime_grants.sql` afterwards (the new tables exist before the default privileges are re-created, so only `runtime_grants.sql` grants on them).
- Local Compose is different on purpose: [`dev/stack/postgres/provision.sql`](../../dev/stack/postgres/provision.sql)
  gives all four roles `LOGIN` and passwords from environment variables, and revokes `CREATE ON DATABASE`
  from `PUBLIC`. The production `provision.sql` does not touch database-level privileges.

Role boundaries are asserted by integration tests and can be checked on a live local stack with
[`dev/stack/postgres/verify-roles.sql`](../../dev/stack/postgres/verify-roles.sql) (the database name
`horizon` is hard-coded).

## Retention and deletion

| Data | When it goes | Mechanism |
|---|---|---|
| Conversation, turns, messages, runs, retry requests, feedback, LangGraph checkpoints | `retention_days` (30) after the conversation's last turn or retry | Chat retention loop: mark `purging`, delete the checkpoint thread, delete the conversation row (cascade). See the [chat page](../services/chat.md#retention) |
| Document, versions, chunks, original object | On `DELETE /v1/documents/{id}` (or when a version is superseded: that version only) | Ingestion cleanup job: exact object versions deleted, chunks deleted, rows tombstoned (see the [ingestion page](../services/ingestion.md#deletion)) |
| Orphaned uploaded objects | After `orphan_grace_seconds` (600) if unreferenced | Ingestion reconciliation loop |
| `users`, `upload_requests`, `ingestion_jobs`, `vendor_permits`, tombstoned `documents`/`document_versions` rows | Never by code | |

Chat retention does not touch documents or users. `ingestion_jobs` and `upload_requests` use plain
(no-cascade) foreign keys and nothing deletes `documents` or `users` rows, so those foreign keys are
never exercised.

## Sizing observations

Vectors are 1024 `float` dimensions per chunk, one chunk per ~1800 new characters (2000-code-point
windows, 200 overlap). The largest accepted document (10,000,000 extracted characters) yields about
5,500 chunks. No table partitioning, archival or vacuum settings are defined in the repository.

## Implementation references

[`libs/schema/src/horizon_schema/models.py`](../../libs/schema/src/horizon_schema/models.py),
[`services/chat/src/horizon_chat/db/`](../../services/chat/src/horizon_chat/db),
[`services/ingestion/src/horizon_ingestion/db/`](../../services/ingestion/src/horizon_ingestion/db),
[`services/chat/src/horizon_chat/db/retention.py`](../../services/chat/src/horizon_chat/db/retention.py).
