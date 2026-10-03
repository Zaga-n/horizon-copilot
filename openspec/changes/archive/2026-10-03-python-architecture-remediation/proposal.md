## Why

The 2026-10-02 Python service architecture audit (`python-service-architecture-audit`) of `services/chat`, `services/ingestion`, and `libs/*` found production-relevant defects that static checks cannot see:

- Ingestion jobs fail permanently after ordinary deploys, outages, or provider slowness.
- Provider and database failures are misclassified in both services.
- One poison item can stall the retention and recovery loops.
- Repositories own business transitions.
- The Bedrock/embedding and OpenTelemetry/logging plumbing is duplicated across the two services and has already drifted.

Static gates are green (14 import-linter contracts kept, ruff/mypy clean, 83 hermetic tests pass), so this work is about semantics, not import direction.

## What Changes

**Behavioral fixes (highest production cost first)**

- **Ingestion attempt counting.** The job attempt budget counts only attempts that actually started. It no longer counts lease reservations or claims ended by worker shutdown. The worker drains running jobs within a shutdown grace period.
- **Ingestion failure classification:**
  - A chunk timeout or dependency outage is retryable, not terminal BUDGET/STORAGE.
  - Each `ConflictError` cause gets its own port error.
  - Bedrock credential, permission, and missing-model errors are classified as *unavailable*.
  - A malformed 2xx response is a provider-protocol failure.
  - `DBAPIError` is split into integrity, data, and connectivity failures.
- **Ingestion retry policy has one owner.** A domain retry-policy decision replaces the duplicated logic in `application/process_job.py` and `workers/dispatch.py`.
- **Chat failure classification:**
  - NUL characters are rejected at input.
  - `DataError`, pool `TimeoutError`, and connection loss are translated.
  - Bedrock `ValidationException` becomes a new *rejected* agent error.
  - Malformed embedding responses and guardrail parse failures are translated inside the agent capability.
- **Batch isolation.** Retention and failure recovery record per-item failures and continue. Only a dependency outage stops a pass.
- **Supervised loops.** Chat recovery and retention loops and the ingestion dispatch loop run under a bootstrap supervisor that owns cadence, failure policy, and health. Readiness reports a stopped loop.

**Ownership fixes**

- Repositories apply transitions decided in `domain/`:
  - chat: run/turn terminal status, lease predicate, `retry_available`;
  - ingestion: job, version, and chunk lifecycle, publication, cleanup, retry/delete eligibility, acceptance replay.
- The `retry_available` rule is unified across the two ingestion modules that currently disagree.
- Run/message ids are minted by the action, not by `observability/tracing.py`. Nondeterminism (clock, uuid, random, sleep) is injected into actions.
- Ingestion GenAI construction moves from `bootstrap/worker.py` into a `genai/` factory. The SDK-client monkey-patch is removed.

**Shared libraries (`libs/`)**

- **New `libs/observability` (`horizon_observability`, observability kind):**
  - OTel provider/resource lifecycle (`open_providers`);
  - the safe JSON log formatter, parameterized by logger prefix, service name, event and field allowlists;
  - `mark_error`;
  - trace-context carrier helpers.
  - Service span vocabulary, `Measurements`, and event names stay in each service.
- **New `libs/genai` (`horizon_genai`, genai kind):**
  - `BedrockConnection` and `bedrock_config` (one physical attempt);
  - `build_bedrock_embeddings` (dimensions, `normalize=True`, credentials);
  - usage capture for Titan responses.
  - Both services build query and document embeddings from this one factory. Ingestion gains optional static AWS credentials, matching chat.
- **Extended `libs/config` (`horizon_config`):** `discover_policy_directory(start, override)` replaces the two copies. Each service keeps its own `Settings` schema and environment selection.
- **New `libs/google-identity` (`horizon_google_identity`, client kind):** Google ID-token verification (signature, issuer, audience, expiry, token-size bound, subject from `sub` only) and the bounded certificate fetch, with library-owned errors. Each service keeps a thin adapter that maps those errors to its own port errors, plus `LocalIdentityVerifier`. Required at two consumers by the extraction table's "must agree" row: if the copies drift, the two services accept different tokens.
- **Not shared:** the `Settings` field overlap stays per service (the architecture rule says each service owns its schema).

**Modularization of large files** (no behavior change)

- Chat:
  - split `db/runs.py` (451 lines), `application/stream_turn.py` (330), and `domain/conversations.py` (274) by aggregate and lifecycle;
  - split `bootstrap/runtime.py` by capability;
  - split `observability/tracing.py` into setup, metrics, and tracing.
- Ingestion:
  - split `db/indexing.py` (378), `db/acceptance.py`, `db/status.py`, `application/process_job.py`, and `workers/dispatch.py`;
  - add `db/tables.py` so sibling `db/` modules stop importing each other's privates.
- Tests:
  - split `tests/integration/test_pipeline.py` (795), `tests/unit/test_agent.py` (560), and `tests/integration/test_streaming.py` (425);
  - shared harnesses move into `*_testing` packages.

No public HTTP route, SSE event, or database schema changes are intended. One error code is added to the turn failure categories (provider rejection).

## Capabilities

### New Capabilities

None. Shared libraries and modularization are structural; their observable effects are captured below.

### Modified Capabilities

These capabilities are still defined in in-flight changes and have not yet been archived to `openspec/specs/`. This change adds requirements to them.

- `document-indexing`:
  - attempt accounting across restarts;
  - retryable classification of timeouts, outages, and provider credential failures;
  - poison-row isolation;
  - one embedding construction contract shared with chat.
- `chat-conversations`:
  - invalid-input rejection (NUL);
  - per-item isolation in retention and recovery;
  - supervised background loops visible to readiness.
- `horizon-agent`:
  - a distinct provider-rejection outcome;
  - every agent-capability failure translated to a named outcome.

## Impact

- **Code:** `services/chat/src/horizon_chat/**`, `services/ingestion/src/horizon_ingestion/**`, `libs/config`; new `libs/observability`, `libs/genai`, and `libs/google-identity`.
- **Workspace:**
  - new uv members;
  - `pyproject.toml` (`known-first-party`, import-linter root packages, independence and importer contracts for the new libraries);
  - `uv.lock`;
  - service Dockerfiles copying the new libs.
- **Dependencies:** `opentelemetry-sdk` and exporters move to `horizon-observability`; `langchain-aws` and `botocore` move to `horizon-genai`. Each service depends on these libs instead of re-declaring them.
- **Operations:**
  - jobs interrupted by deploys no longer exhaust their budget;
  - readiness can report stopped loops;
  - new error category for chat provider rejection;
  - ingestion accepts optional `INGESTION_AWS_*` static credentials.
- **Audit artifacts:** `SKILL-GAPS-2026-10-02.md` at the repo root.
