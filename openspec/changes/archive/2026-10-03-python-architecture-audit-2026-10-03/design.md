## Context

This change implements the verified findings of the 2026-10-03 `python-service-architecture-audit`; proposal.md has the motivation. That audit verified the unarchived `python-architecture-remediation` change, whose 39 tasks are all checked.

Current state that shapes the approach:

- **Gates are green.** 23 import-linter contracts are kept; Ruff, format and mypy are clean; 312 non-live tests pass. Every item here is semantic or behavioral.
- **A concurrent session edited the same files during the audit** (2026-10-03 09:46–09:50):
  - it moved `ingestion/db/indexing.py` and `db/status.py` into packages;
  - it rewrote `ingestion/api/errors.py` as one `PUBLIC_ERRORS` table;
  - it made `DataIntegrityError` subclass `RejectedError` in both services;
  - it added chat's error-mapping exhaustiveness test.

  Those edits resolved two chat findings, which are excluded here. The forwarding coordinators survived the move into packages. Every task re-checks its evidence against the tree before editing.
- **Rule sources:** `python-service-architecture` (SKILL.md and references) and `python-service-architecture-audit`. The cited sections are listed per finding in the evidence table below.

## Goals / Non-Goals

**Goals:**
- Close the five behavioral Violations first, with tests that fail before the fix.
- Remove the forwarding `db/` layers without breaking the ~400-line signal.
- Return status choice, outcome recording and error ownership to their owners.
- Leave each service with no static-script coordinator notices, and a semantic audit rerun with no Violations.

**Non-Goals:**
- No database schema change, public route change or SSE shape change.
- No change to retry budgets, lease lengths or chunking parameters, except the bounds the specs require.
- Not extracting `ports/errors.py`, `db/transactions.py`, `config/secrets.py` or the supervisors into libraries (see the shared-capability conclusions).
- Not changing the exact-revision readiness check. Its rolling-deploy risk is already recorded in the remediation design.
- The chat history N+1 feedback query is tracked by remediation task 10.3, not here.

## Evidence and classification

Paths are relative to each member's package. Violations are listed in the order they will be fixed.

| ID | Class | Finding | Evidence | Rule |
|---|---|---|---|---|
| V1 | Violation (behavioral) | Chat loop crash: never restarted; `/health` constant 200 | `chat bootstrap/supervisor.py:64-66`, `api/routers/health.py:12`, `db/transactions.py` returns `None` for `ProgrammingError` | api-and-workers.md "Long-running worker", "Health and readiness" |
| V2 | Violation (behavioral) | NUL in extracted text → `DataError` → INTEGRITY → terminal; NUL in metadata → 422 after S3 write (orphan) | `ingestion domain/chunking.py:59`; probe: psycopg `DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes` | boundaries.md "Validate external structure" |
| V3 | Violation (behavioral) | Cleanup/delete jobs retry forever; `failed` exit reachable only from tests | `ingestion domain/retry_policy.py:59-71`, `application/process_job.py:51`, `integration/test_deletion.py:145` | persistence.md "Every durable intermediate state has an exit" |
| V4 | Violation (behavioral) | Worst-case shutdown ≈70 s vs 40 s stop grace; `release` undercounts a chunk left `processing` | `ingestion application/index_version.py:94-95,121-130`, `compose.yaml:249`, `config/base.yaml:7-8` | api-and-workers.md "Long-running worker" |
| V5 | Violation (behavioral) | `ValidationException` always rejected; malformed 2xx → INTERNAL; chat retriever folds rejected/protocol into unavailable and retries; guardrail parse → unavailable; retrieval pool timeout / undecodable row → INTERNAL; lost lease → INTERNAL | `libs/genai errors.py:45-47`, `chat genai/retrieval/retriever.py:31-41,100-105`, `middleware/guardrail.py:46-50`, `db/retrieval.py:88-120`, `db/run_attempts.py:48-54`, `ingestion genai/embeddings.py:69-76` | ai.md "Invocation and error translation", "Tool rules" |
| V6 | Violation | Corrupt queue row failed outside `failure_settlement`, logged at warning; decode `ValidationError` escapes untranslated | `ingestion db/queue.py:84-99`, `db/indexing/manifests.py`, `db/status/queries.py` | boundaries.md "Validate external structure", "Repositories apply decisions" |
| V7 | Violation | Same-named transaction coordinators | `chat db/runs.py:45-125`; `ingestion db/indexing/store.py`, `db/status/store.py` | SKILL.md rule 6; boundaries.md "No forwarding layers" |
| V8 | Violation | Entry point calls two application functions | `chat api/routers/turns.py:27,47,66`; `ingestion workers/jobs.py:9,66-68` | SKILL.md rule 2; boundaries.md "Action boundaries" |
| V9 | Violation | Repositories choose status literals | `ingestion db/job_control→status/control.py`, `indexing/publication.py`, `indexing/cleanup.py`, `indexing/chunk_progress.py`, `indexing/manifests.py`, `acceptance.py:258,277`, `queue.py`; `chat db/run_admission.py:66,86,160,171,259` | boundaries.md "Repositories apply decisions" |
| V10 | Violation | Telemetry inside actions; OTel `Span` in `Admitted`; `Telemetry \| None = None` | `chat application/stream_turn.py`, `submit_turn.py:20-24,46-103`; `ingestion application/index_version.py`, `cleanup_document.py`, `process_job.py:92-105`, `job_context.py:35` | boundaries.md `observability/`, "Constructor contracts" |
| V11 | Violation | Errors with wrong owners | `chat domain/agent.py:48-65` (not on bases); `ingestion ports/identity.py` (no application importer); `StatusStore` raising `ports/uploads` errors | boundaries.md "Errors and constants follow ownership", "Application ports"; errors.md "Translate once" |
| V12 | Violation | No `horizon_config` importer contracts | `pyproject.toml` (only independence contract at :208-212) | python-repository-setup pre-commit.md "Architecture contracts" |
| V13 | Violation | Chat bootstrap calls `build_bedrock_embeddings` | `chat bootstrap/runtime.py:50,206-210` | shared-libraries.md kinds table (genai row) |
| V14 | Violation (low) | Ingestion routes raise `HTTPException(422, "<code>")`, numeric status codes, flat `api/` | `ingestion api/documents.py:65-108,166,182` | api-and-workers.md "Public error mapping", "Thin routes" |
| I1 | Improvement | `libs/schema` declares unused `sqlmodel`, `psycopg`; holds role/grant SQL | `libs/schema/pyproject.toml:7-8`, `libs/schema/sql/*.sql` | shared-libraries.md "Dependencies"; sqlmodel-alembic repo-layout.md |
| I2 | Improvement | Migrations runner only upgrades; overlap untested | `services/migrations main.py:23-33` | sqlmodel-alembic alembic-migrations.md "Runner commands" |
| I3 | Improvement | `TurnContext` collaborator bundle (≈100 lines of `NotImplementedError` fakes in one unit test) | `chat application/turn_context.py`, `tests/unit/test_turn_admission.py:33-139` | SKILL.md "Entry points pass collaborators explicitly" |
| I4 | Improvement | Bootstrap hygiene | chat checkpoint store built twice (`runtime.py:88,127`), loops/`ProcessHealth` owned by integration runtime, unused `Runtime.agent`/`checkpoints`, unused `capture_content`; ingestion listener unsupervised (`bootstrap/worker.py:88,106`), Bedrock client ownership reason unrecorded | boundaries.md `bootstrap/`; ai.md "Ownership inside genai/<task>/" |
| I5 | Improvement | Near-duplicate `Telemetry.work` span lifecycle | `chat observability/tracing.py:95-115`, `ingestion observability/tracing.py:67-92` | otel shared_library.md |
| I6 | Improvement | Ingestion job deadline counts permit waits; inline budget guard beside `decide_job_failure`; HTTP paths built in `domain/lifecycle.py:18-23` | as cited | api-and-workers.md; domain.md |

Rejected candidates are recorded so the next audit does not reopen them:
- `RetentionJob` and `ReconciliationJob` meet trigger 4.
- `UploadStream` is implemented structurally by `UploadFile` and has test doubles.
- The psycopg imports in chat bootstrap only build the engine.
- The long repository methods apply domain decisions.
- `ports/errors.py` is duplicated per service on purpose, as errors.md "Classification bases" requires.
- Repeated literals are log and span keys.

## Decisions

**D1. Liveness reads `ProcessHealth`; the loop is not restarted in-process (V1).**
- `/health` depends on a narrow `Liveness` Protocol in `api/dependencies.py`. The lifespan owner creates `ProcessHealth` and passes it to both the supervisor and the app.
- Alternative: restart a crashed loop in-process with backoff. Rejected: a defect that crashes once crashes again, and api-and-workers.md assigns restart to the platform.
- Chat's `translate_database_error` maps `InsufficientPrivilege` to unavailable, matching `db/checkpoints.py`, so a missing grant backs off instead of crashing.

**D2. Retention iteration returns `bool` "more due"; shutdown uses `asyncio.timeout(grace)` (V1, spec "Bounded shutdown").**
- The supervisor's `Iteration` type becomes `Callable[[], Awaitable[bool]]`. Recovery returns `False`.
- The grace period is a chat setting validated against `compose.yaml`'s stop grace in a settings test.

**D3. Sanitize extracted text in `domain/chunking`; reject control characters in `UploadMetadata` and filename validation (V2).**
- One pure function, `strip_control(text)`, applied to text units and the title before the window build.
- **Fingerprint:** widen `Pipeline.normalization` to `Literal["crlf-cr-to-lf", "crlf-cr-to-lf+c0"]` with the new default. Widen rather than replace, because stored `pipeline_configuration` rows must keep decoding (the V6 trigger). Effects:
  - the content-match conflict query in `db/acceptance.py:148` stops matching versions indexed under the old pipeline, so identical content re-uploaded as a new document is accepted, not reported as a duplicate, until the old version is re-indexed;
  - replays with the same idempotency key still replay, because they compare the request fingerprint, not the pipeline;
  - existing ready documents are not re-indexed automatically.
- Alternative: escape NUL in the repository. Rejected: it changes the evidence text the model sees and hides the rule in `db/`.

**D4. Cleanup cap reuses `max_job_attempts` for permanent categories (V3).**
- `decide_job_failure` gains a cleanup branch: transient → `Retry`; permanent with attempts ≥ the cap → `Fail`.
- The existing delete re-queue in `db/status/control.py` then becomes reachable. The README documents the discovery query (`lifecycle = 'deleting'` with job `failed`).
- Alternative: log at ERROR forever. Rejected: no exit, and an alert alone cannot unblock the document.

**D5. Validate the shutdown budget in settings; reset the chunk on the last failed call (V4).**
- A `model_validator` on worker settings asserts `shutdown_grace + connect + read + release ≤ stop_grace_seconds`. `stop_grace_seconds` is a new setting mirrored into `compose.yaml` (`stop_grace_period: 75s`).
- `index_version` calls `retry_chunk` on the final failed call attempt, so `release` decrements only in-flight chunks.
- Alternative: bound the post-cancel wait below the provider timeout. Rejected: an abandoned in-flight call's outcome is uncertain, which persistence.md "Uncertain external writes" says to avoid.

**D6. Classification lives once per owner (V5).**
- **`libs/genai classify_bedrock_error`:** a `ValidationException` whose message names the model identifier ("model identifier", "model ID", "on-demand throughput") → unavailable; otherwise rejected. The match is on the message, because AWS has no separate code for it. Unknown messages default to rejected, the current behavior, and a unit test pins each known message.
- **The ingestion embedding adapter and the chat retriever** catch `AttributeError`/`TypeError`/`ValueError` from the response path as protocol errors, and `checked_vector` validates the element types.
- **Chat retriever, tool and runner:** each carries a three-way error family (`...UnavailableError`, `...RejectedError`, `...InvalidOutputError`), and `ToolRetryMiddleware` retries only unavailable. Guardrail parse failure → `AgentOutputError` (invalid output).
- **`db/retrieval.py`:** catches the pool `TimeoutError` and `OSError` as unavailable; decodes per row, skipping and logging undecodable rows. `Locator` decodes with `extra="ignore"` on the read side only; the writer's model stays strict.
- **Lost lease:** `ConversationBusyError` from `held_run` during an attempt maps to INTERRUPTED in `fail_turn.FAILURE_CATEGORIES`.

**D7. Corrupt jobs go through `failure_settlement` (V6). This keeps the in-flight requirement.**
- The remediation spec "Poison job isolation" requires corrupt jobs to be marked failed. boundaries.md says to leave corrupt records for an operator. The product requirement is explicit, so the claim path calls the same domain `failure_settlement` with category INTEGRITY and logs at ERROR. The conflict is recorded in `SKILL-GAPS-2026-10-03.md`.
- Decode errors in the version, chunk and status reads raise `IndexStoreIntegrityError(DataIntegrityError)` carrying the row id. The listing decodes per row and reports a corrupt row with an integrity status.

**D8. Fold forwarding modules into the port implementations; split ports by action usage, not by file length (V7).**
- **Ingestion:**
  - `CleanupStore` (cleanup refs and finish) becomes its own port, implemented in `db/cleanup.py`; `cleanup_document` is its only user.
  - `PgIndexStore` absorbs manifests, chunk progress and publication, about 300 lines in `db/indexing.py`.
  - `PgStatusStore` absorbs queries and control, about 280 lines in `db/status.py`.
  - The packages created by the concurrent session collapse back to modules (flat-first).
- **Chat:** `SqlRunLedger` owns its transactions. Shared locked reads (`owned_run`, `held_run`, `reserve_run`) move to `db/ledger.py`. Admission and attempt functions become methods. `AdmissionPolicy` is removed (fields read from `self`). If the file passes ~400 lines, split `RunLedger` by lifecycle (admission vs attempt) into two ports, each implemented directly.
- Alternative: keep the modules and accept the coordinator. Rejected: rule 6 forbids it by name; see the skill gap on the conflicting signals.

**D9. Streaming is a private step of the turn actions (V8, V10).**
- `submit_turn`/`retry_turn` return `Replayed | Admitted(events: AsyncIterator[StreamEvent])`.
- The streaming body moves to `application/_turn_stream.py` with `prepare_attempt` and `fail_turn` as private helpers.
- The checkpoint-start decision becomes `domain/runs.checkpoint_start(plan) -> CheckpointStart`.
- A `TurnObservation` helper in `observability/` owns span start/end, error marking, duration and first-token metrics. The action passes it outcome values and never touches `.span`.
- Ingestion: `process_job` returns `JobOutcome = Ready | Retrying | Failed | Fenced | Released` and records defect outcomes itself; `workers/jobs.run_claim` logs and records metrics from the outcome.

**D10. Statuses come from domain transition values (V9).**
- Domain functions return typed transitions (for example `RestartStates(job=QUEUED, chunks=PENDING, version=CANDIDATE)`, `PublicationStates`, `CleanupStates`), plus constructors for initial states.
- `db/` writes `transition.job.value`.
- A fitness test greps `db/` for business-status string literals in `values(...)`/`update(...)` calls.

**D11. Error ownership (V11).**
- Chat agent errors move to `ports/agent.py`, each subclassing a classification base.
- Ingestion `IdentityVerifier` and its errors stay in `ports/identity.py`, and the port gets its application consumer: a one-call `application/identity.authenticate` action, as in chat. (Superseded 2026-10-03: moving the contract into `api/dependencies.py` makes `adapters/identity.py` import `api/`, which the audit script reports as a VIOLATION.) `StatusStore` raises `StatusNotFoundError`/`StatusConflictError` defined in `ports/indexing.py`.
- Retrieval errors stay in chat `domain/retrieval.py` (skill gap: a private cross-boundary Protocol has no rule for its errors).

**D12. Contracts and wiring (V12–V14).**
- One `horizon_config` importer contract per service, forbidding it everywhere except `config` and `bootstrap`.
- Chat `genai/retrieval/embeddings.py:build_query_embeddings(client, model_id, dimensions)` wraps the library factory. `bedrock_runtime_client` stays in bootstrap, because bootstrap owns the client lifecycle (skill gap).
- Ingestion routes move to `api/routers/documents.py` and `api/routers/health.py`, raise named validation errors mapped in `PUBLIC_ERRORS`, and use `fastapi.status` names.

## Shared-capability conclusions

| Candidate | Conclusion |
|---|---|
| `ports/errors.py` (byte-identical) | Stays local by rule (errors.md "Classification bases"); skill gap recorded for a third service |
| `adapters/identity.py`, `ports/identity.py`, health routes, middleware, dependencies, `bootstrap/app.py`, `db/readiness.py`, `config/settings.py`, `observability/genai.py` | Stay local: different meaning or thin per-service mapping |
| `Telemetry.work` | Improvement I5: `horizon_observability.spans.boundary_span(tracer, name, *, carrier=None)`; boundary literals and `Measurements` stay local; migrate ingestion (superset) first |
| `open_telemetry` resource identity | Optional; not in this change |
| `config/secrets.py` validators | Stay local (two copies, nothing must agree; skill gap on settings fragments) |
| `db/transactions.py` | Align the `OSError` handling, or comment why the copies differ; no extraction (≈25 lines, would need a database-runtime library) |
| `bootstrap/supervisor.py` | Stays local (different crash lifecycle) |
| Chat `Locator` vs ingestion writer | Not extracted: the reader tolerates unknown fields (D6). Extract to a contract library only if a third reader appears |

## Risks / Trade-offs

- **[Risk] Liveness restarts mask a crash loop.** → The crash is logged once with `loop_crashed`. Restart counts are visible on the platform, and the privilege fix removes the known trigger.
- **[Risk] ValidationException message matching is brittle.** → Unknown messages keep today's behavior (rejected). Pinned tests document each matched phrase, and a `live`-marked test checks a wrong model id against Bedrock.
- **[Risk] The new fingerprint allows duplicate content across documents for already-indexed versions.** → This is accepted and documented; the conflict check resumes once versions are re-indexed. The alternative, keeping the old fingerprint, would hide the normalization change from manifests.
- **[Risk] Folding modules re-creates large files.** → Each port is split by action usage when it passes ~400 lines (D8), never by forwarding.
- **[Risk] Concurrent sessions edit the same files.** → Tasks are ordered by boundary, and each starts by re-reading its evidence. Do not run this change while another session is restructuring the same services.
- **[Trade-off] `stop_grace_period` grows to 75 s.** → Rollouts are slower, but no attempt is counted for a shutdown.

## Migration Plan

Every step is a deployable slice. Rollback is a redeploy of the previous image; there are no schema migrations. Order:
1. Contracts and libraries (V12, V5 library part).
2. Behavioral fixes:
   - chat V1 → V5;
   - ingestion V2 → V3 → V4 → V6 → V5.
3. Forwarding removal (V7). Ownership follows: V8 + V10 together per service, then V9, V11.
4. Wiring (V13, V14).
5. Improvements.

Operational notes:
- Set the liveness probe on `/health` after step 2.
- Raise `stop_grace_period` before deploying the validator, or the worker refuses to start.

## Implementation notes (apply, 2026-10-03)

Decisions taken while implementing, where the code differs from the text above:

- **Provider-protocol category (V5).** The spec requires a named provider-protocol outcome, so `FailureCategory.PROVIDER_PROTOCOL` was added. `AgentProtocolError` subclasses `AgentOutputError`, as D6 asks, and maps to that category instead of `INVALID_CITATIONS`. A malformed 2xx chat-model body is translated at the model call by a `ModelProtocolTranslation` middleware inside `ModelRetryMiddleware`, so a `ValueError` elsewhere in the graph stays a defect.
- **`checkpoint_start` (D9)** returns a `CheckpointSource` decision (`RECORDED`, `CURRENT`, `PREDECESSOR`, `EMPTY`), not a `CheckpointStart`: the current checkpoint id needs I/O, which `domain/` cannot do.
- **`TurnObservation` (D9, V10)** replaces `RootAttempt`; it owns the root span, the turn outcome logs and the agent metrics. Actions call its outcome methods and never touch the span.
- **`JobOutcome` (D9)** is `Ready | Retrying | Failed | Fenced | Unrecorded`. There is no `Released`: cancellation keeps propagating after the claim is released. Defects are now settled against the started claim's counters.
- **7.3:** the admission unit test's fakes cover exactly the ports `submit_turn` uses; streaming is part of the action, so every passed port is used.
- **7.4:** `Runtime.agent` and `Runtime.checkpoints` stay; after 5.1 the turn routes pass them to the actions. The ingestion listener runs under `run_supervised(required=False)`: a crash is logged and periodic scans continue.
- **7.6:** the job deadline excludes vendor-permit waits and recorded backoffs (`JobDeadline` reschedules the job timeout); `BUDGET` stays terminal for real overruns. URLs are built by `api/schemas.py` response bodies; the wire JSON is unchanged.
- **7.2 found a deadlock:** two overlapping migration jobs deadlocked when one held the checkpoint lock while LangGraph ran `CREATE INDEX CONCURRENTLY`, which waits for the other job's statement blocked in `pg_advisory_lock`. The checkpoint lock is now polled with `pg_try_advisory_lock` within the 60 s budget.
- **Retention batches (D2):** more work is due only after a full batch that purged something, so a batch of nothing but failures waits for the next interval instead of retrying back to back.

## Remaining risks (after the 2026-10-03 rerun)

- **Exact-revision readiness** during a rolling deploy is unchanged (recorded in the remediation design).
- **Bedrock message matching:** a model-identifier `ValidationException` is recognized by its message. Pinned unit tests cover the known phrases; no live test ran in this change, so a reworded AWS message falls back to "rejected".
- **Model-call `ValueError`:** a `ValueError` raised by LangChain's own conversion code at the model call is reported as `provider_protocol` rather than `internal`.
- **Fingerprint change:** versions indexed before `crlf-cr-to-lf+c0` no longer match new uploads of the same content until re-indexed (D3).
- **Liveness restarts:** a defect that crashes a chat loop on every start becomes a restart loop; it is visible as `loop_crashed` logs and platform restart counts.
- **Migration lock wait:** a second migration job waits at most 60 s for the checkpoint lock, then exits nonzero instead of waiting indefinitely.
- **Failed superseded cleanup** has no API re-queue; it is listed by the README query and needs an operator.
- **Ingestion missing grants:** ingestion's `translate_database_error` still re-raises `InsufficientPrivilege` as a defect; chat now treats it as an outage.
