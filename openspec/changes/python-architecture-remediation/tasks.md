## 1. Ingestion behavioral fixes

- [x] 1.1 Add `ports/errors.py` (DependencyUnavailable with retry_after, Rejected, Integrity bases). Move `DependencyUnavailableError`/`StaleClaimError` out of `ports/uploads.py`. Turn `EmbeddingError(retryable=...)` into subclasses, and replace the message-string checks (`no_extractable_text`, `file_too_large`) with typed errors. Verify: `lint-imports` passes and a unit test asserts each subclass's classification.
- [x] 1.2 Split `db/transactions.py` error mapping:
  - IntegrityError → integrity;
  - DataError → rejected;
  - Operational/Interface/pool TimeoutError/invalidated connection → unavailable.

  Verify: an integration test for a constraint violation gets an integrity error, not `database_unavailable`.
- [x] 1.3 Add `domain/retry_policy.decide_job_failure` and use it from both `application/process_job.py` and `workers/dispatch._record_failure`. Delete the duplicated backoff and logs. Verify: unit tests cover chunk timeout → retry, outage → retry, ConflictError causes → no BUDGET, and the terminal content categories. Exactly one `ingestion_job_failed` log per failure.
- [x] 1.4 Treat a chunk timeout as retryable, and move the vendor-permit wait and the `next_retry_at` sleep out of the chunk deadline. Verify: a unit test with a slow fake embedding yields a scheduled retry.
- [x] 1.5 Give each `ConflictError` cause its own port error (`chunk_not_processing`, `manifest_incomplete`, `empty_manifest`), mapped by the retry policy. Verify: the stale-claim scenario test from the document-indexing spec.
- [x] 1.6 Move attempt counting from the claim in `db/queue.py` to a fenced `start_attempt` write, release claims on shutdown cancellation, and add a `shutdown_grace_seconds` policy key with a settings validator. Verify: an integration test with three cancel-before-settle cycles leaves the job eligible, and a drain test records the outcome before exit.
- [x] 1.7 Validate claimed rows per row, and fail corrupt ones with an integrity category instead of letting a ValidationError crash the worker. Verify: the poison-row integration test (corrupt `trace_context` ahead of a valid job).
- [x] 1.8 Remove the blanket `except ValueError` → 422 in `api/documents.py:59,101`, keeping only metadata validation errors. Verify: an API test where an unrelated ValueError yields a 500 and malformed metadata yields a 422.

## 2. Chat behavioral fixes

- [x] 2.1 Add `ports/errors.py` bases. Have `db/transactions.py` and `db/readiness.py` translate DataError (rejected), pool `TimeoutError`, and invalidated connections (unavailable). Verify: unit tests with a fake engine, and `/ready` returns 503 on pool timeout.
- [x] 2.2 Reject `\x00` in `TurnInput.content` and the feedback comment models. Verify: an API test where a NUL-containing turn returns a validation error and creates no rows.
- [x] 2.3 Add `AgentRejectedError` and a `PROVIDER_REJECTED` failure category, mapping Bedrock ValidationException in `genai/horizon_agent/runner.py`. Verify: a runner unit test for ValidationException → rejected and ThrottlingException → unavailable.
- [x] 2.4 Translate malformed embedding responses (ValueError) in `genai/retrieval/retriever.py` and guardrail structured-output parse failures in `middleware/guardrail.py`. Map `SearchIntegrityError` to an index-integrity category. Verify: unit tests for each failure, and no INTERNAL category.
- [x] 2.5 Isolate per-item failures:
  - `db/retention.py`: record a degraded conversation and continue; don't re-select it first;
  - `application/recover_failures.py`: continue past an integrity error.

  Verify: integration tests for both scenarios in the chat-conversations spec.
- [x] 2.6 Move run/message id minting into the action through `id_factory`, and move the version constants to `genai/`. `Telemetry.reserve` receives the ids. Verify: an action unit test with a deterministic id_factory.

## 3. Domain-owned transitions

- [x] 3.1 Chat: create `domain/runs.py` (moving the run types out of `domain/conversations.py`) with `terminal_transition`, `retry_available`, and `lease_held`. Have `db/runs.py`, `db/ledger.py`, and `db/conversations.py` apply them through one terminal writer. Verify: domain unit tests for each transition, and the existing ledger integration tests still pass.
- [x] 3.2 Ingestion: create `domain/lifecycle.py` (publication, cleanup, retry/delete eligibility, one `retry_available`) and `domain/acceptance.py` (fingerprint, replay/replacement). Have `db/queue|indexing|status|acceptance` apply them. Verify: domain unit tests, plus an integration test showing `retry_available` agrees between upload replay and status.
- [x] 3.3 Inject `clock`/`id_factory`/`jitter`/`sleep` into the ingestion actions, and pass the upload deadline as a duration compared on the DB clock. Verify: `audit_service.py` shows no Nondeterminism notices for `application/`.

## 4. Shared configuration mechanics

- [x] 4.1 Add `discover_policy_directory(start, override)` to `horizon_config`, with unit tests. Replace both service copies; services keep reading `HORIZON_CONFIG_DIR`. Verify: `audit_service.py --library configuration` passes and the settings tests pass in both services.

## 5. `libs/genai` (horizon_genai)

- [x] 5.1 Scaffold `libs/genai` (pyproject, `py.typed`, README stating the extraction trigger), register it in the workspace, isort known-first-party, and import-linter root packages. Add contracts: independence from services, and importers limited to `genai/` and `bootstrap/`. Verify: `uv lock --check` and `lint-imports` pass.
- [x] 5.2 Implement:
  - `BedrockConnection`/`bedrock_config`;
  - `build_bedrock_embeddings`;
  - `build_bedrock_chat_model`;
  - the `TitanUsage` decorator;
  - library errors and `classify_bedrock_error`.

  Verify: a unit table test of the code → class mapping (403/AccessDenied/ExpiredToken/ResourceNotFound → unavailable, ValidationException → rejected, malformed 2xx → protocol), and `audit_service.py --library genai` passes.
- [x] 5.3 Migrate ingestion:
  - add optional `INGESTION_AWS_*` secrets with chat's pairing validator;
  - add a `genai/embeddings.py` factory, built in `to_thread`;
  - remove the inline construction and the monkey-patch from `bootstrap/worker.py`;
  - map library errors once into ingestion port errors;
  - use `horizon_schema.EMBEDDING_DIMENSIONS`.

  Verify: the pipeline integration tests pass and a unit test asserts static credentials reach the client.
- [x] 5.4 Migrate chat: delete `genai/shared/llms.py` and `genai/retrieval/llms.py::build_embeddings` in favor of the library, and map library errors once in the retriever and runner. Verify: `test_model_factory.py` and the retrieval tests pass.

## 6. `libs/observability` (horizon_observability)

- [x] 6.1 Scaffold `libs/observability` with the workspace entry, contracts (independence; importers limited to `observability/` and `bootstrap/`), and dependencies on the OTel SDK and exporters. Remove those dependencies from the service pyprojects. Verify: `lint-imports` and `audit_service.py --library observability` pass.
- [x] 6.2 Implement `ResourceIdentity`, `open_providers` (ParentBased(ALWAYS_ON), bounded shutdown), `JsonLogFormatter`, `install_json_logging`, `mark_error`, `outcome_of`, and the carrier helpers. Verify: library unit tests with in-memory exporters, covering redaction and dropped fields.
- [x] 6.3 Migrate chat `observability/` to the library, keeping `Measurements`, `Boundary`, and the event and field allowlists local. Verify: `tests/unit/test_observability.py` passes unchanged.
- [x] 6.4 Migrate ingestion `observability/` the same way. Verify: `tests/unit/test_observability.py` passes, and cancellation is still reported as `cancelled`.

## 7. `libs/google-identity` (horizon_google_identity)

- [x] 7.1 Scaffold `libs/google-identity` (pyproject with `google-auth` and `httpx`, `py.typed`, README stating the extraction trigger and the security rationale). Register it in the workspace, isort known-first-party, and import-linter root packages. Add contracts: independence from services, and importers limited to `adapters/` and `bootstrap/`. Verify: `uv lock --check`, `lint-imports`, and `audit_service.py --library client` pass.
- [x] 7.2 Move `GoogleIdTokenVerifier`, `CertificateRequest`, `HttpCertificateRequest`, and `VerifiedClaims` into the library with library-owned errors, and move `tests/unit/test_identity.py` coverage into the library's tests. Verify: library unit tests cover a bad signature, wrong audience, expired token, oversized token, missing `sub`, and certificate fetch failure → `CertificatesUnavailableError`.
- [x] 7.3 Migrate chat: `adapters/identity.py` keeps only the error-mapping wrapper and `LocalIdentityVerifier`, and bootstrap builds the library verifier. Verify: the chat identity tests and the API auth tests pass, and `google.oauth2` is no longer imported in `horizon_chat` (grep).
- [x] 7.4 Migrate ingestion the same way and remove `google-auth` from both service pyprojects. Verify: the ingestion identity and upload auth tests pass, and `uv lock --check` passes.

## 8. Supervised loops and readiness

- [x] 8.1 Add `bootstrap/supervisor.py` (cadence, outage backoff, crash logging, `ProcessHealth`) in chat. Convert `workers/recovery.py` and `workers/retention.py` to iteration functions. Readiness ANDs in `loops_alive()`. Verify: a unit test where a crashing iteration makes readiness not-ready.
- [x] 8.2 Move the ingestion dispatch loop cadence, stop, and drain into a supervisor. Move reconciliation to `db/reconciliation.py` as a technical job, and stop building the API identity client in the worker. Verify: the dispatch tests and the drain test from 1.6 pass.
- [x] 8.3 Remove `TurnService` and expose `submit_turn`/`retry_turn` returning `Replayed | Admitted`. The turns route no longer calls the conversations port or ends spans. Verify: `test_streaming.py` and `test_conversation_api.py` pass.

## 9. Modularization (moves only)

- [x] 9.1 Chat:
  - `db/runs.py` → `run_admission.py`, `run_attempts.py`, `ledger.py`;
  - `application/stream_turn.py` → `submit_turn.py`, `stream_turn.py`, `fail_turn.py`;
  - `domain/conversations.py` → `conversations.py`, `runs.py`, `feedback.py`, `sources.py`;
  - `bootstrap/runtime.py` → per-capability builders;
  - `observability/tracing.py` → `setup.py`, `metrics.py`, `tracing.py`.

  Verify: no source file over 400 lines (`wc -l`), and the full test suite passes.
- [x] 9.2 Ingestion:
  - add `db/tables.py`;
  - `db/indexing.py` → `manifests.py`, `chunk_progress.py`, `publication.py`, `cleanup.py`, `reconciliation.py`;
  - `db/acceptance.py` → `owners.py` + `acceptance.py`;
  - `db/status.py` → `status_queries.py` + `job_control.py`;
  - `application/process_job.py` → `index_version.py` + `cleanup_document.py` + the `db/vendor_permits.py` decorator;
  - `workers/dispatch.py` → `jobs.py` + `reconciliation.py`.

  Verify: no `db/` module imports a sibling's `_`-prefixed name (grep), and the full test suite passes.
- [x] 9.3 Tests:
  - split `test_pipeline.py`, `test_agent.py`, and `test_streaming.py` per Design Decision 9;
  - move harnesses to `horizon_*_testing`;
  - add unit tests for `process_job`, `dispatch`, and `upload_document`.

  Verify: the `pytest --collect-only` count is ≥ the previous count, and no test file is over 400 lines.

## 10. Completion gate

- [x] 10.1 Rerun `audit_service.py` on both services (with `--workspace .`) and in `--library` mode on config, schema, genai, and observability. Verify: 0 violation candidates, and each remaining review notice is either dismissed in design.md or fixed.
- [x] 10.2 Run `scripts/quality.sh` and the integration profile (`pytest -m "not live"` with Postgres and MinIO). Verify: all pass.
- [x] 10.3 Record in design.md the remaining semantic risks that static checks cannot prove: readiness requiring the exact migration revision during rolling deploys, and the per-message feedback query in history.
