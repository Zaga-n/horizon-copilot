## Why

The 2026-10-03 `python-service-architecture-audit` verified the `python-architecture-remediation` change. It found five production-relevant behavioral defects:
- a crashed chat loop takes every replica out of rotation permanently;
- a NUL byte in extracted PDF text fails a document forever;
- cleanup jobs retry without bound;
- ingestion shutdown can outlast the container stop grace;
- provider and retrieval failures are still folded into the wrong class.

The audit also found that the remediation's file splits reintroduced forwarding `db/` coordinators.

Static gates are green: 23 import-linter contracts kept, Ruff and mypy clean, and 312 non-live tests passing against Postgres, MinIO and the migrations image. The remaining work is semantic.

## What Changes

**Behavioral fixes (highest production cost first)**

- **Chat loop liveness and restart.** Liveness (`/health`) reads the supervisor's health state, so an orchestrator restarts a process whose maintenance loop crashed. A missing privilege (`InsufficientPrivilege`) is translated as unavailable in every chat store, not only in checkpoints.
- **Bounded chat shutdown.** Retention purges one batch per iteration and reports whether more is due. Shutdown waits for the loops under a grace timeout, then cancels them.
- **Ingestion text and metadata sanitation.**
  - NUL and other C0 control characters (except tab and newline) are removed from extracted text and titles before chunking. The pipeline normalization version changes, so the fingerprint changes too.
  - Control characters in a filename, corpus or metadata are rejected with 422 before any object is stored.
- **Cleanup has an exit.** Cleanup and delete jobs with a permanent failure category stop after a bounded number of attempts and record `failed`. That makes the existing repeated-DELETE re-queue reachable, and the stuck state can be found by query.
- **Ingestion shutdown fits the stop grace.**
  - Drain grace + provider timeout + release timeout ≤ the deploy stop grace, enforced by a settings validator.
  - A chunk whose last call attempt failed is reset, so `release` never undercounts it.
- **Failure classification:**
  - `libs/genai`: a Bedrock `ValidationException` naming the model identifier is unavailable; only input-caused validation is rejected.
  - Malformed 2xx bodies (non-object JSON, non-numeric vectors) are provider-protocol failures in both services.
  - Chat: embedding rejection and invalid output stay distinct from unavailable through the retriever, the tool and the runner, and only the unavailable class is retried. Guardrail parse failures are invalid output. Retrieval pool timeouts and `OSError` are unavailable. A stored chunk row that cannot be decoded is skipped and logged as an integrity fault instead of failing the turn as INTERNAL. A lost turn lease ends as INTERRUPTED.
- **Corrupt queue rows** in ingestion are still failed with an integrity category, as the in-flight "Poison job isolation" requirement demands. They now go through the domain failure settlement, so the version and chunks settle too, and they are logged at error. Decode failures of version, chunk and status rows are translated to an integrity error, and one corrupt row no longer fails a whole document listing.

**Ownership fixes**

- **Forwarding layers removed.** `chat/db/runs.py`, `ingestion/db/indexing/store.py` and `ingestion/db/status/store.py` stop wrapping same-named module functions in `transaction()`. Each port implementation runs its own queries. Ingestion's `IndexStore` gives up cleanup to a separate `CleanupStore` port.
- **One action per entry point.**
  - Chat turn routes call only `submit_turn`/`retry_turn`. Streaming becomes their private step, and the checkpoint-start decision moves to `domain/runs`.
  - The ingestion worker calls only `process_job`, which records defect outcomes itself and returns a typed `JobOutcome`.
- **Statuses decided in `domain/`.** Domain transition functions return target states, and `db/` writes enum values instead of choosing literals.
- **Telemetry out of actions.**
  - Actions return outcome values; entry points and observability helpers record metrics and logs.
  - No OTel `Span` crosses the application boundary.
  - `Telemetry` is a required constructor argument.
- **Errors with their owners.**
  - Chat's `HorizonAgent` errors move to `ports/agent.py` on the classification bases.
  - Ingestion's `IdentityVerifier` contract moves beside its only consumer in `api/`.
  - Ingestion's `StatusStore` gets its own error classes.
- **Contracts and wiring.**
  - Add the missing `horizon_config` importer contracts.
  - Chat builds query embeddings through a `genai/retrieval` factory, not by calling the library factory from bootstrap.
  - Ingestion routes raise port errors mapped by `PUBLIC_ERRORS`, use `fastapi.status` names, and move to `api/routers/`.

**Improvements**

- `libs/schema` drops the unused `sqlmodel` and `psycopg` dependencies. Provisioning and grant SQL moves to `services/migrations`.
- The migrations runner exposes `check` and `sql` as well as `upgrade`. A test proves two runners do not overlap.
- Chat's `TurnContext` is replaced by explicit keyword ports plus a frozen `TurnPolicy`.
- Bootstrap hygiene:
  - build the chat checkpoint store once;
  - make the lifespan own loops and `ProcessHealth`;
  - supervise the ingestion listener;
  - record the reason the Bedrock client is hand-built.
- `horizon_observability` gains a `boundary_span` helper that both services' `Telemetry.work` uses.

No public HTTP route, SSE event shape or database schema changes. **BREAKING (operational):** the liveness endpoint can now fail, and ingestion rejects uploads whose filename or metadata contain control characters.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

These capabilities are still defined in in-flight changes and have not been archived to `openspec/specs/`, so this change adds requirements to them, as `python-architecture-remediation` did.

- `chat-conversations`:
  - liveness reflects crashed loops;
  - bounded shutdown;
  - privilege errors classified as unavailable.
- `horizon-agent`:
  - distinct invalid-output and rejected classes through retrieval;
  - only transient failures retried;
  - corrupt evidence rows isolated;
  - lost lease ends as interrupted.
- `document-indexing`:
  - control characters sanitized or rejected;
  - cleanup failure exit;
  - shutdown fits the stop grace;
  - corrupt queue rows left for operators;
  - model-identifier and malformed-response classification.

## Impact

- **Code:**
  - `services/chat/src/horizon_chat/**`
  - `services/ingestion/src/horizon_ingestion/**`
  - `services/migrations/**`
  - `libs/genai`, `libs/observability`, `libs/schema`
  - root `pyproject.toml` (importer contracts), `compose.yaml` (stop grace, liveness probe), `conftest.py` and README (moved SQL)
- **Operations:**
  - an orchestrator liveness probe on `/health` now restarts a process with a crashed loop;
  - ingestion `stop_grace_period` must cover the validated drain budget;
  - the pipeline normalization fingerprint changes; design.md records the effect on existing manifests and on upload replay;
  - cleanup jobs can now reach `failed`.
- **Dependencies:** removed from `horizon-schema`; nothing added.
- **Audit artifacts:** `SKILL-GAPS-2026-10-03.md` at the repository root.
