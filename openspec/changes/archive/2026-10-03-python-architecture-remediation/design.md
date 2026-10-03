## Context

Source: the 2026-10-02 `python-service-architecture-audit` run. Motivation: see proposal.md.

**Static checks.** `audit_service.py` reported 0 violation candidates and 23 (chat) / 29 (ingestion) review notices; both libraries passed. `lint-imports` keeps all 14 contracts and CI runs it through `scripts/quality.sh`. Every finding below comes from semantic tracing and was confirmed in code.

### Ownership matrix (abridged)

| Action | Entry | Port → impl | Hops / modules | Transition owner today → target |
|---|---|---|---|---|
| chat submit/retry turn | `api/routers/turns.py` | RunLedger → `db/runs.py` | 4 / 7 | `db/runs.py` → `domain/runs.py` |
| chat stream turn | turns route + `api/sse.py` | RunLedger, CheckpointStore, HorizonAgent | ~25 modules | `db/runs.finish`/`fail_pending` → domain `terminal_transition` |
| chat recover failures | `workers/recovery.py` (loop in bootstrap TaskGroup) | RunLedger | 4 / 5 | repository → domain; loop → supervisor |
| chat retention (technical job) | `workers/retention.py` | `db/retention.py` | 3 / 4 | unchanged; per-item isolation added |
| chat rag_search tool | `genai/horizon_agent/tools.py` | Retriever → EvidenceIndex → `db/retrieval.py` | 3 | evidence budget moves into the Retriever |
| ingestion upload | `api/documents.py` | Preparer, Storage, AcceptanceStore | 7 | `db/acceptance` → `domain/acceptance.py` |
| ingestion poll/retry/delete | `api/documents.py` | StatusStore → `db/status.py` | 5 | `db/status` → `domain/lifecycle.py` |
| ingestion process_job | `workers/dispatch.run_claim` | IndexStore, WorkQueue, Extraction, Embedding, Storage | ~11 | split across process_job, dispatch, queue, indexing → `domain/retry_policy.py` + `domain/lifecycle.py` |
| ingestion reconcile (technical job) | dispatch timer | `IndexStore.reconcile` | 5 | → `db/reconciliation.py` called directly by the job |

The only forwarding hops are public one-call actions (`application/conversations.py`, `application/control_documents.py`). The rules allow these.

### Static REVIEW hits dismissed after reading the code

- `CertificateRequest`: a private narrowing of a transport, with a test double.
- `RetentionJob`: workers may not import `db/`.
- `EvidenceIndex` in `genai/retrieval`: ai.md prescribes this placement.
- psycopg imported in bootstrap.
- `UploadStream`: implemented structurally by Starlette's `UploadFile`.
- Unicode digits in `Retry-After`: `float()` accepts them and the value is capped.
- Repeated `job_id` string literals: they are log keys.

## Goals / Non-Goals

**Goals**
- Close the behavioral violations first. Each fix ships with a failing-first test.
- Domain functions own transitions; repositories apply them.
- Two shared libraries of a single *kind* each, which consumers adopt one at a time.
- No module over ~400 lines, and no `db/` module importing a sibling's private helpers.

**Non-Goals**
- No shared `Settings` base class or shared settings schema.
- No change to HTTP routes, SSE events, or the database schema.
- No general `common`/`shared` package.

## Decisions

### Decision 1: `libs/observability` (`horizon_observability`, kind=observability)

**Evidence.**
- `observability/tracing.py::open_telemetry` and `observability/logging.py::JsonFormatter`/`configure_logging` are identical in both services, apart from the service name, logger prefix, and allowlists.
- The copies have drifted: chat uses an `ALWAYS_ON` sampler while ingestion uses the default parent-based sampler, and only ingestion treats `CancelledError` as `cancelled` in `Telemetry.work`.
- shared-libraries.md says two copies that are meant to be the same but have drifted are a **Violation**.

**Public API** (explicit typed inputs, no environment reads):
- `ResourceIdentity(namespace, name, version, instance_id, environment)`.
- `open_providers(identity, endpoint, sampler) -> AsyncContextManager[Providers(tracer_provider, meter_provider)]`. Shutdown is bounded and runs in `to_thread`.
- `JsonLogFormatter(service_name, logger_prefix, events, fields, full_exception_trace)` and `install_json_logging(formatter, level)`. The library itself configures no logging; bootstrap calls `install_json_logging`.
- `mark_error(span, exc)`, `outcome_of(exc)` (`cancelled` vs `error`), `inject_carrier()`, `extract_link(carrier)` (the traceparent/tracestate allowlist and the 512-character bound move here from ingestion).

**Stays in each service:** the `Boundary` literals, `Measurements`, `Telemetry.work`/`reserve`, `RootAttempt`, event and field allowlists, the GenAI callback handlers, and HTTP middleware.

**Sampler:** both services use `ParentBased(ALWAYS_ON)`. Chat's run roots already start from an empty `Context()`, so their behavior does not change.

**Importers:** `observability/` and `bootstrap/` only. This is enforced by a new import-linter contract that forbids `horizon_observability` everywhere else in each service, plus an independence contract.

*Alternative considered:* also share `Telemetry.work`. Rejected, because its `Boundary` vocabulary and `Measurements` are service-owned.

### Decision 2: `libs/genai` (`horizon_genai`, kind=genai)

**Evidence.**
- Chat has `genai/shared/llms.py` (`BedrockConnection`, `bedrock_config`) and `genai/retrieval/llms.py::build_embeddings`.
- Ingestion builds `BedrockEmbeddings` inline in `bootstrap/worker.py:41-53`, without static credentials and on the event loop. It also monkey-patches `embeddings.client = UsageClient(...)`.
- The two services write and read the same vectors, so the model id, dimensions, and `normalize=True` form a cross-service compatibility contract with one owner.
- This is a duplicate that has drifted, so it is a **Violation**.

**Public API:**
- `BedrockConnection` (secret fields `repr=False`) and `bedrock_config(connection)` (one physical attempt).
- `build_bedrock_embeddings(connection, model_id, dimensions) -> Embeddings`.
- `build_bedrock_chat_model(connection, model_id, *, reasoning_effort, max_tokens, streaming, callbacks, tags)`, a thin `init_chat_model` wrapper so the connection policy has one owner.
- `TitanUsage`: an `Embeddings` decorator that reads `inputTextTokenCount` through a caller-supplied `on_usage(int)` callback, replacing the SDK monkey-patch. It takes no telemetry type.

**Library errors:** `GenAIProviderUnavailableError(retry_after)`, `GenAIProviderRejectedError`, `GenAIProtocolError`, plus `classify_bedrock_error(exc)`, which encodes the code table once:
- throttling, 5xx, timeouts, AccessDenied, UnrecognizedClient, ExpiredToken, and ResourceNotFound → unavailable;
- ValidationException → rejected;
- a malformed 2xx → protocol.

Each service's `genai/` translates these library errors exactly once into its own port errors (chat `AgentUnavailableError`/`AgentRejectedError`/`RetrievalUnavailableError`; ingestion `EmbeddingError` subclasses).

**Stays in each service:** prompts, schemas, middleware (budget, guardrail, retries), `Retriever`, `TitanEmbeddings`'s port mapping, and model-id settings. The embedding model id and dimensions keep their single owner, `horizon_schema.EMBEDDING_DIMENSIONS`. Ingestion's hard-coded `1024` is replaced with that constant.

**Importers:** `genai/` and `bootstrap/` only. The audit's library table lists only `genai/`, but bootstrap needs `BedrockConnection` to map settings; this is recorded as a skill gap.

*Alternative considered:* put embeddings in `horizon_schema`. Rejected, because that would mix the persistence kind with I/O.

### Decision 3: `horizon_config.discover_policy_directory(start: Path, override: str | None) -> Path`

- The two copies of policy-directory discovery are identical, and `horizon_config` is already a dependency of both services. The extraction table says to move such a helper into the existing library now.
- Each service still reads the `HORIZON_CONFIG_DIR` environment variable and passes its value in.
- Environment selection, validators, and the `Settings` schemas stay in each service: the configuration-mechanics rules make services own them.
- The overlapping fields (aws_region, timeouts, database pool, log, otlp, identity) are left duplicated. This is a deliberate decision, not an oversight.

### Decision 4: `libs/google-identity` (`horizon_google_identity`, kind=client)

**Evidence.**
- `adapters/identity.py` is byte-identical in both services (79 lines).
- `ports/identity.py` differs only in docstrings.
- Both services verify against the same `google_client_id` audience.
- The extraction table's "two copies enforce one rule both sides must agree on" row applies, so this is a **Violation**: if one copy fixes a verification rule and the other does not, one service accepts tokens the other rejects. The same row also covers the embedding parameters in Decision 2.

**Public API** (explicit inputs, no environment reads, one attempt per call):
- `GoogleIdTokenVerifier(audience, request)`, with `async verify(token: str | None) -> str` (the subject). Contains the 16384-character token bound, `sub`-only identity, and `asyncio.to_thread` around the blocking google-auth call.
- `CertificateRequest` (private narrowing Protocol) and `HttpCertificateRequest(client: httpx.Client)`. The client is *borrowed*: bootstrap creates and closes it.
- Errors: `GoogleIdentityError` (base), `InvalidIdTokenError` (rejected), `CertificatesUnavailableError` (unavailable). google-auth, `ValueError`, and pydantic failures are translated once, `from exc`.

**Stays in each service:**
- `ports/identity.py` (`IdentityVerifier`, `InvalidIdentityError`, `IdentityUnavailableError`).
- A thin `adapters/identity.py` that wraps the library verifier and maps its errors to the port errors exactly once.
- `LocalIdentityVerifier`, which is development-only and gated by each service's settings validator.
- Bootstrap's choice between local and Google mode.

**Importers:** `adapters/` and `bootstrap/` only. Enforced by an import-linter contract per service and an independence contract (no `horizon_chat`/`horizon_ingestion`).

**Dependencies:** `google-auth` and `httpx` move from the service pyprojects into the library. Services keep `httpx` only if they use it elsewhere.

*Alternative considered:* keep two copies with a sync comment. Rejected: a comment does not stop drift in security-sensitive code, and the extraction cost is small.

### Decision 5: Domain-owned transitions

**Chat: new `domain/runs.py`.** Move `AttemptStatus`, `TurnStatus`, `FailureCategory`, `Run`, `Turn`, `RunIdentity`, `Admission`, `Checkpoint*`, `ensure_retryable`, and `same_input` here, and add:
- `terminal_transition(attempt_outcome) -> (AttemptStatus, TurnStatus)`;
- `retry_available(turn)`;
- `lease_held(run, now)`.

`db/runs.py` and `db/ledger.py` apply these results through one terminal writer.

**Ingestion: new domain modules.**
- `domain/lifecycle.py`: job, version, and chunk transitions, publication outcome, cleanup outcome, retry and delete eligibility, and one `retry_available`.
- `domain/retry_policy.py`: `decide_job_failure(category, attempt, policy, retry_after) -> Retry(delay) | Terminal(category)`.
- `domain/acceptance.py`: request fingerprint, replay/replacement decisions.

`workers/dispatch._record_failure` calls the same decision, and the log is emitted once.

### Decision 6: Attempt accounting

- Claiming reserves the job (increments `generation` and sets the lease) but does not increment `attempts`/`cycle_attempts`.
- A fenced `start_attempt` write increments them when processing actually begins (after readiness and the permit). On shutdown cancellation the job is released (`lease_until = now()`) and the counters stay unchanged.
- The supervisor stops claiming on the stop event and waits up to `shutdown_grace_seconds`, a new policy key that must exceed the heartbeat interval, before cancelling.
- The schema is unchanged: the same columns keep different write timing.

### Decision 7: Supervisor

Add `bootstrap/supervisor.py` in each service:
- `run_supervised(name, iteration, interval, stop, health)` owns cadence, backoff on a dependency outage, crash logging, and a `ProcessHealth` flag.
- Workers become iteration functions that each call one action and log its summary.
- Readiness ANDs the database check with `health.loops_alive()`.

### Decision 8: Error bases

Each service adds `ports/errors.py` with `DependencyUnavailableError` (with `retry_after`), `RejectedError`, and `IntegrityError` bases.

- Ingestion's `EmbeddingError(retryable=...)` becomes subclasses of these bases.
- `DependencyUnavailableError` and `StaleClaimError` move out of `ports/uploads.py`.
- Classifying errors by matching message strings (`str(exc) == "no_extractable_text"`, `"file_too_large"`) is replaced by typed errors.
- `db/transactions.py` in both services maps:
  - `IntegrityError` → integrity;
  - `DataError` → rejected;
  - `OperationalError`, `InterfaceError`, pool `TimeoutError`, and `DBAPIError` with `connection_invalidated` → unavailable;
  - everything else → re-raised.

### Decision 9: Module splits

Splits are behavior-preserving moves, done after the behavioral fixes so the diffs stay reviewable. Each split commit contains moves and import updates only.

**Chat**
- `db/runs.py` → `db/run_admission.py`, `db/run_attempts.py`, and a public `db/ledger.py`.
- `application/stream_turn.py` → `submit_turn.py` (returns `Replayed | Admitted`, so the route stops branching or calling the port), `stream_turn.py`, and `fail_turn.py`. `TurnService` is removed; the runtime exposes actions.
- `domain/conversations.py` → `conversations.py`, `runs.py`, `feedback.py`, and `sources.py`.
- `bootstrap/runtime.py` → `_build_database`, `_build_agent`, `_build_identity`, plus `supervisor.py`.
- `observability/tracing.py` → `setup.py` (thin over the library), `metrics.py`, and `tracing.py`.

**Ingestion**
- `db/indexing.py` → `db/tables.py`, `manifests.py`, `chunk_progress.py`, `publication.py`, `cleanup.py`, and `reconciliation.py`.
- `db/acceptance.py` → `db/owners.py` and `db/acceptance.py`.
- `db/status.py` → `db/status_queries.py` and `db/job_control.py`.
- `application/process_job.py` → `index_version.py` and `cleanup_document.py`; permit gating becomes an EmbeddingPort decorator in `db/vendor_permits.py`.
- `workers/dispatch.py` → `workers/jobs.py` and `workers/reconciliation.py`, with the loop moved to the supervisor.
- `adapters/documents.py` split: optional (Preference).

**Tests**
- `test_pipeline.py` → `test_indexing_pipeline.py`, `test_deletion.py`, `test_work_queue.py`, `test_reconciliation.py`, and `test_http_e2e.py`, with the Harness moved to `horizon_ingestion_testing/pipeline.py`.
- `test_agent.py` → guardrail, citations, budget/retries, and streaming test files, with helpers moved to `horizon_chat_testing/agent.py`.
- `test_streaming.py` → `test_turn_http.py` and `test_turn_failures.py`.

`libs/schema/models.py` (509 lines, 14 `Table` definitions) is left as one module. Table metadata is declarative and is reviewed as one schema revision, so splitting it is a Preference.

### Decision 10: Nondeterminism and ids

- Actions receive `clock`, `id_factory`, `jitter`, and `sleep` arguments.
- `Telemetry.reserve` takes ids from the action. `agent_version` and `retrieval_version` constants move to `genai/`.
- Ingestion upload deadlines are compared on the database clock: the action passes a duration, not an absolute time.

## Risks / Trade-offs

- **Three new libraries add workspace and Docker plumbing.** → Each is one kind with ≤3 modules. Dockerfiles copy `libs/` as a whole.
- **Moving attempt counting can mask a genuine crash-loop** (a job that crashes the process before `start_attempt` is never counted). → `start_attempt` runs before any vendor call, and a separate `reservations` counter caps runaway claims and reports them on the existing failure metric. Note: `generation` already increments and is used for fencing only.
- **Reclassifying credential failures as retryable delays terminal feedback for real misconfiguration.** → Retries are bounded by `max_job_attempts`. Readiness/startup probes a single embedding call when `environment_name != local`. Repeated `provider_unavailable` triggers the existing failure log.
- **Large splits conflict with in-flight work** (`horizon-agent-backend` 24/27 tasks). → Phase splits last and land each split as its own commit after the behavioral phases.
- **Sampler unification changes chat sampling semantics on paper.** → Behavior is the same because roots use an empty context. Covered by the existing `test_observability.py` assertions.

## Migration Plan

1. Behavioral fixes inside the services (no new packages). Each lands independently with tests.
2. Domain transition ownership. Repositories apply the decisions.
3. `horizon_config.discover_policy_directory`.
4. `libs/genai`: migrate ingestion first (it fixes the credential and monkey-patch violations), then chat.
5. `libs/observability`: chat first, then ingestion.
6. `libs/google-identity`: chat first, then ingestion. Auth behavior is unchanged, and the existing identity and API auth tests are the guard.
7. Supervisor and readiness.
8. Module and test splits.

Every phase finishes with `scripts/quality.sh`, the integration profile, a rerun of `audit_service.py` on each member, and `--library` mode on the new libraries.

**Rollback:** each phase is a separate commit series with no schema migration. Revert per phase.

## Implementation Record (2026-10-02)

### Audit rerun (task 10.1)

`audit_service.py --workspace .` reports **0 violation candidates** for both services. `--library` mode passes for `config` (configuration), `schema` (persistence), `genai`, `observability` and `google-identity` (client). The remaining review notices were read and dismissed:

- **Transaction-owning port methods** (`db/runs.py`, `db/indexing.py`, `db/status.py`: "only opens a transaction around a same-named call"). Each port implementation method owns the unit of work and the driver-error translation in `transaction()`. The transition logic lives in per-aggregate modules (`run_admission`, `run_attempts`, `manifests`, `chunk_progress`, `publication`, `cleanup`, `status_queries`, `job_control`) as functions on one connection, so a method never spans two transactions.
- **Bootstrap collaborator counts** (chat `build_runtime` 15, ingestion `run_worker` 12). Database, agent, identity, turn-context, job-context and maintenance-loop builders were extracted. What remains is one-line store construction and the resource lifecycle (telemetry, database, storage, Bedrock client, listener) that the composition root must own.
- **Repository method length** (chat `history`, `turn`, `PgvectorEvidenceIndex.search`; ingestion `accept`, `_existing`, `claim`). The remaining length is SQL and row mapping; the decisions they apply now come from `domain/runs.py`, `domain/acceptance.py` and `domain/lifecycle.py`.
- **`ports/errors.py` identical in both services.** These are per-service port contracts: errors.md places the classification bases in each service's `ports/errors.py`. Two identical copies are expected, not drift.
- **Single-implementation Protocols in `workers/`** (`RetentionJob`, `ReconciliationJob`). These exist because `workers/` may not import `db/` (an existing dismissal).
- **Pre-existing dismissals** still apply: psycopg in bootstrap, `UploadStream`, `ports/identity.py` consumed by the API boundary, repeated log-key string literals, and `Any` in LangChain callback signatures (framework-imposed).

### Remaining semantic risks (task 10.3)

- **Readiness requires the exact migration revision.** Both services report not-ready unless `app.alembic_version` equals `horizon_schema.SCHEMA_REVISION`. During a rolling deploy, pods of the previous release go unready as soon as the migration job advances the revision, and new pods are unready until it does. Every schema change therefore needs an expand/contract sequence or a deploy order that tolerates a brief readiness gap. Static checks cannot prove the order is right.
- **Per-message feedback query in history.** `SqlConversationStore.history` issues one feedback query per message on the page (bounded by the page size, at most 100). This is an N+1 pattern that can dominate history latency for long pages; a single joined query would remove it.

### Decisions made during implementation

- **No `reservations` counter.** The schema is unchanged, so a process that crashes between claim and `start_attempt` can still be reclaimed without bound. `start_attempt` runs before any vendor call to keep that window small. On cancellation, `release` un-counts the started attempt and resets the in-flight chunk's attempt.
- **No startup embedding probe.** The risk mitigation above mentions one; it was not implemented. Misconfigured credentials surface as bounded `provider_unavailable` retries and failure logs.
- **Retry policy.** Transient categories retry until `max_job_attempts`, after which the job fails with the *actual* cause (for example `provider_unavailable`), not `budget_exhausted`. Content causes (`no_extractable_text`, `extraction_failed`, `provider_rejected`, `integrity_failed`) are terminal immediately. Cleanup jobs always retry. New ingestion categories: `provider_protocol`, `manifest_incomplete`, `integrity_failed`. A stale chunk completion is a fenced stop.
- **Chunk deadline** counts provider time only, accumulated across physical calls. Permit waits, recorded backoffs and retry sleeps are excluded.
- **Upload deadline** is the stored object's age on the database clock (`max_object_age_seconds`), the same clock and reference point orphan reconciliation uses, rather than an absolute request deadline from the application clock.
- **No `db/vendor_permits.py` embedding decorator.** Gating permits inside `EmbeddingPort.embed` would put the permit wait back inside the chunk deadline (task 1.4). Permit gating stays in `application/index_version.py`.
- **Two new chat failure categories.** Besides `provider_rejected`, `index_integrity` is added, as the horizon-agent spec requires. Malformed embedding responses and unparseable guardrail output are `service_unavailable`. A provider rejection of a query embedding fails the search as unavailable.
- **Titan usage** is observed through a botocore `after-call` event hook (`observe_titan_usage`) on a client that bootstrap creates and closes, not through an `Embeddings` decorator. `build_bedrock_chat_model` constructs `ChatBedrockConverse` directly, so `horizon-genai` does not depend on `langchain`.
- **Embedding dimensions.** Ingestion takes `embedding_dimensions` from settings, and its readiness check rejects a value that differs from `horizon_schema.EMBEDDING_DIMENSIONS`. The persistence-model contract forbids importing `horizon_schema` outside `db/`.
- **Revoked checkpoint privileges** (`InsufficientPrivilege`) are classified as unavailable: they stop the retention pass rather than marking each conversation degraded. A checkpoint-deletion timeout is also unavailable.
- **Supervisors.** A crashed chat loop is recorded in `ProcessHealth` and turns readiness off while the API keeps serving. A crashed ingestion worker loop stops the worker (`WorkerLoopCrashedError`) so the orchestrator restarts it, because the worker exposes no readiness endpoint.
- **Extra modules** beyond the decision list: chat `domain/values.py` (the shared `StrictModel`, needed to split `domain/conversations.py` without an import cycle) and `application/turn_context.py` (the dependency bundle that replaces `TurnService`); ingestion `application/job_context.py`, `application/failures.py`, `db/fencing.py` and `db/owners.py`. The chat run ledger implementation stays in `db/runs.py`; `db/ledger.py` keeps the shared row primitives, including the single terminal writer `end_attempt`.
- **Dockerfiles** copy each new library explicitly, following the existing per-member style, rather than copying `libs/` as a whole.
