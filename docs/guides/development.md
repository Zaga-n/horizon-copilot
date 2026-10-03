# Development

How to set up the repository, where things live, the rules your change must satisfy, and recipes for the
common kinds of change. For running tests see [Testing](testing.md); for the local stack see
[Local deployment](local-deployment.md).

## Toolchain

| Tool | Version | Source |
|---|---|---|
| Python | 3.13.16 | [`.python-version`](../../.python-version); every member requires `>=3.13,<3.14` |
| uv | 0.12.13 | `[tool.uv] required-version` in [`pyproject.toml`](../../pyproject.toml); one root `uv.lock` |
| Node / npm | Node 24 (`engines` allows `>=22.12`) | `services/frontend`, its `package-lock.json` |
| Docker + Compose v2.20+ | | Local stack, integration tests |

```zsh
uv sync --locked --all-packages        # every Python member plus dev tools
uv run --locked pre-commit install     # commit and push hooks
scripts/quality.sh                     # full local quality gate
```

Production-style installs select one member and omit dev dependencies, for example
`uv sync --locked --no-dev --package horizon-chat`.

## Repository map

```text
services/chat/        FastAPI chat service (hexagonal: api, application, domain, ports, adapters, db, genai, …)
services/ingestion/   Upload API + always-on worker (same layout; both services have workers/)
services/migrations/  One-shot Alembic + LangGraph checkpoint job, SQL provisioning/grants
services/frontend/    React/Vite SPA (own npm toolchain; excluded from the uv workspace)
libs/                 Shared Python libraries: config, genai, google-identity, observability, schema
config/               Layered YAML policy (base, environment, per service)
dev/stack/            Local Collector, Alloy, Grafana, MinIO/Postgres provisioning, smoke canary
scripts/              quality.sh, mypy-members.sh
openspec/changes/     Spec-driven change records (proposal, design, specs, tasks)
```

Python deployables use `services/<name>/src/horizon_<name>` with tests in `services/<name>/tests`.

## Rules your change must satisfy

All enforced by `scripts/quality.sh`, pre-commit (commit: Ruff, lock check, import-linter; push: mypy,
hermetic tests) and CI:

- **Ruff** (line length 100, target py313): includes bug-bear, simplify, complexity ≤ 10, no relative imports,
  no `assert` outside tests, no `print`, timezone-aware datetimes, blind-except and several others; see
  `[tool.ruff.lint]`.
- **mypy `strict`** per member with the pydantic plugin; `warn_unreachable`.
- **import-linter**: layering inside each service and the library boundaries ([Chat service](../services/chat.md#code-layout-and-layer-rules),
  [Shared libraries](../architecture/shared-libraries.md)). A new import that crosses a boundary fails the
  gate: move the code rather than loosening the contract.
- **Pytest** with `--strict-markers`; unit tests unmarked, infrastructure tests marked `integration`.
- **Database vocabularies**: SQL code must use the domain enums, never literal status strings (a fitness test
  checks it).
- **Lockfile**: after changing any `pyproject.toml` dependency run `uv lock` and commit `uv.lock`; the gate
  runs `uv lock --check`.

Frontend: `npm run lint`, `npm run typecheck`, `npm test`, `npm run test:browser` (CI jobs `Frontend quality`
and `Frontend browser`).

## Change recipes

Each recipe lists the files to touch and the checks to run. None was executed as a dry run.

### Add or change a chat endpoint

1. Router: [`api/routers/conversations.py`](../../services/chat/src/horizon_chat/api/routers/conversations.py)
   (or `turns.py`, or a new router included in `bootstrap/app.py::create_app`). A route calls exactly one
   `application/` action.
2. New capability: a Protocol in `ports/`, an implementation in `db/` or `adapters/`, a property on
   `api/dependencies.py::ChatApiRuntime` and a field on `bootstrap/runtime.py::Runtime`.
3. New error: define it in `ports/errors.py` (or a port), map it in
   `api/exception_handlers.py::PROBLEMS`; `test_api_errors.py::test_every_request_error_is_mapped` fails
   otherwise.
4. Tests: unit under `services/chat/tests/unit`; behaviour needing a database under `tests/integration`.
5. Update [API reference](../reference/api.md).

### Add or change an ingestion endpoint

Router `api/routers/documents.py`, models in `api/schemas.py`, public error codes in
`api/errors.py::PUBLIC_ERRORS`; application action in `application/`; database access in `db/`. Keep
cross-owner access returning `not_found`. Tests: `tests/unit/test_api_errors.py`, `tests/integration/test_http_e2e.py`
style tests. Update the [API reference](../reference/api.md).

### Add a setting

1. Add the field to the service's `Settings` class (`config/settings.py`). A field **without a default and not
   in `ENV_ONLY_FIELDS`** is policy and may appear in YAML; deployment topology belongs in `ENV_ONLY_FIELDS`.
   Add validators for cross-field constraints.
2. Policy default in `config/base.yaml` (shared) or `config/services/<service>.yaml`; for a new
   environment-only input add it to the service `.env.example` and, if Compose should pass it, to
   `compose.yaml`.
3. Consume it in `bootstrap/` (the composition root) and pass it down explicitly; domain code should not read
   settings.
4. Tests in `services/<service>/tests/unit/…settings…`; update [Configuration](../reference/configuration.md).
5. Restart is the only way to apply a new value.

### Add a database change

Follow [Migration job → Adding a revision](../services/migrations.md#adding-a-revision): edit `libs/schema`,
add an Alembic revision, bump `SCHEMA_REVISION`, add table handles and **explicit grants** in
`runtime_grants.sql`, and update [Data model](../reference/data-model.md).

### Change the agent (tool, middleware, prompt)

- Tool: builder in `genai/horizon_agent/tools.py` (pattern: `build_rag_tool`), register it in
  `agent.py::build_agent`, extend `runner.progress_phase` and the error translation. Arbitrary tools are an
  explicit non-goal of the design.
- Middleware: subclass `AgentMiddleware` in `genai/horizon_agent/middleware/` and insert it in the
  `build_agent` list (order matters; wrap hooks run outer to inner).
- Prompts or behaviour changes: bump `AGENT_VERSION` (in `agent.py`) and `PROMPT_VERSION` (in `prompts.py`); they are stored on every run.
- Tests: use the scripted models in `tests/horizon_chat_testing` (`runner(...)` builds the real compiled graph).
  Offline tests cannot prove real model behaviour.

### Add a failure category

- Chat: `FailureCategory` in `domain/runs.py` and the mapping `FAILURE_CATEGORIES` in
  `application/_turn_stream.py`. The database column is free text, so no migration is needed; document it in
  [Events](../reference/events.md).
- Ingestion: `ErrorCategory` in `domain/documents.py`, `TERMINAL_CATEGORIES` in `domain/retry_policy.py`,
  the exception map in `application/failures.py`.

### Add a metric, span or log event

Define it where the service defines the others (`observability/metrics.py` / `observability/tracing.py` for
chat, `observability/tracing.py` for ingestion). Then:

- **Log events and fields** are allowlisted in the service's `observability/logging.py` (`EVENTS` and field
  sets); an unregistered snake_case event keeps its name with `event.unregistered=true`, and any other
  message text from service code is never written.
- **Span and metric attributes** must also be added to the Collector allowlists in
  [`dev/stack/collector/config.yaml`](../../dev/stack/collector/config.yaml), or they are dropped before
  storage; span names also need to match `filter/genai` to reach Langfuse.
- Never attach prompts, documents, tokens or identifiers as metric attributes.
- Update dashboards in `dev/stack/grafana/dashboards/` and [Observability](../operations/observability.md).

### Frontend feature

Add a module under `src/features/<name>/` with its own components, hook and model; compose it in
`app/App.tsx`; use `shared/api.ts` for requests (new backend calls need matching backend support and CORS
methods). Add Vitest tests next to pure logic and a Playwright spec for user flows (fixtures in
`tests/fixtures.ts`).

### Add a shared library

Only when two deployables must agree on a rule ([Shared libraries](../architecture/shared-libraries.md)):
new `libs/<name>` package, workspace membership (automatic via `libs/*`), the package in `root_packages` and its contracts in the import-linter configuration, in Ruff's isort `known-first-party` and in coverage `source` (all in `pyproject.toml`), a `COPY libs/<name> libs/<name>` line in each consuming Dockerfile, and a `COPY …/pyproject.toml` line in every Dockerfile that runs `uv sync --locked` against the workspace.

## Specifications and agent tooling

Larger changes are recorded as OpenSpec changes under [`openspec/changes/`](../../openspec/changes)
(`proposal.md`, `design.md`, `specs/`, `tasks.md`; schema `spec-driven` in
[`openspec/config.yaml`](../../openspec/config.yaml)). Specifications state intent; where they disagree with
the code, the code and this manual describe actual behaviour (for example the design's maintenance schedule
differs from the interval loop that was built). Project-local skills for coding agents live in
`.agents/skills` (also exposed as `.claude/skills`); MCP servers for agents are listed in `.mcp.json`. Open
engineering items are tracked in [`TODO.md`](../../TODO.md).
