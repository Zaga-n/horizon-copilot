# Testing

What test suites exist, how to run each, what infrastructure they need, and what they do to state.
Commands run from the repository root unless a working directory is stated.

## Tiers

| Tier | Selector | Needs | Covers |
|---|---|---|---|
| Quality gate | `scripts/quality.sh` | uv, Python 3.13.16 | Lock freshness, Ruff, import boundaries, strict mypy per member, test discovery, hermetic tests |
| Hermetic Python tests | `pytest -m "not integration and not e2e and not live"` | nothing external | Unit tests (unmarked) and the `contract` migration-chain test |
| Python integration | `pytest -m integration` | Disposable PostgreSQL with pgvector; disposable MinIO for ingestion; Docker for the container variants | Real database, real object store, real parsers, the real FastAPI apps; models and embeddings faked |
| Frontend unit | `npm test` (in `services/frontend`) | Node | Decoder, API client, identity, document models |
| Frontend browser | `npm run test:browser` (in `services/frontend`) | Node, Chromium | Chat, documents and accessibility flows against mocked Google and API contracts |

Markers (declared in the root `pyproject.toml`, enforced with `--strict-markers`): `integration`,
`contract`, `e2e`, `live`. **No test is marked `e2e` or `live`**; there is no automated live-AWS or
signed-in test, so Bedrock access, quotas and real Google tokens are only exercised by the manual
procedures in [Smoke test](smoke-test.md). `services/ingestion/tests/integration/test_http_e2e.py` is an
`integration` test.

## Setup

```zsh
uv sync --locked --all-packages
```

Requires uv 0.12.13 (pinned by `[tool.uv] required-version`) and Python 3.13.16 (`.python-version`).
This installs every workspace member and the shared dev group (pytest, Ruff, mypy, import-linter,
pre-commit). The frontend has its own toolchain (below).

## Quality gate and hermetic tests

```zsh
scripts/quality.sh
```

Runs, in order: `uv lock --check`; `ruff check` and `ruff format --check` over `services libs conftest.py`;
`lint-imports` (the import-linter contracts); `scripts/mypy-members.sh` (strict mypy per member, skipping the frontend, plus the root `conftest.py`); `pytest --collect-only -q`; then `pytest -m "not integration and not e2e and not live"`.
Extra arguments go to the final pytest call (CI passes `--junitxml=artifacts/python-unit.xml`). No
infrastructure is touched. Expected result: every step exits 0.

Targeted runs:

```zsh
uv run --locked pytest services/chat/tests/unit
uv run --locked pytest services/ingestion/tests/unit
uv run --locked pytest -m contract
uv run --locked pytest libs
```

Install the pre-commit hooks once with `uv run --locked pre-commit install` (installs both hook types):
at commit, Ruff check and format check, `uv lock --check` and `lint-imports`; at push, mypy per member
and the hermetic pytest selection. There are no frontend hooks.

## Python integration tests

Integration tests create and drop their own databases but need an **admin connection to a disposable
PostgreSQL database whose name ends in `_test`**; the fixture refuses anything else
(`Refusing a target other than PostgreSQL with a *_test database`). Do not point it at the Compose
database or any shared server.

What each test does (root `conftest.py::migrated_database`): connect as admin, `CREATE DATABASE
horizon_test_<random>`, apply `services/migrations/sql/provision.sql`, run Alembic `upgrade head` as
`app_migrator`, yield the DSN, and `DROP DATABASE … WITH (FORCE)` afterwards. The admin role must be able
to create databases, roles and the `vector` extension. Without `TEST_DATABASE_DSN` the tests skip;
`REQUIRE_INTEGRATION=1` turns skips into failures.

### Provision disposable infrastructure

The commands below use local containers on uncommon ports so they do not collide with the Compose
stack. The MinIO images must already be cached or available through authenticated registry access.
CI builds the matching MinIO releases from pinned upstream source commits instead (see [CI](#ci)).
On Docker Desktop the host name seen from inside a container differs from the Linux runner.

```zsh
docker run -d --name horizon-test-postgres \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=horizon_test \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17

docker run -d --name horizon-test-minio \
  -p 127.0.0.1:59000:9000 \
  -e MINIO_ROOT_USER=test-admin -e MINIO_ROOT_PASSWORD=disposable-test-password \
  quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z server /data

ready=0
for attempt in {1..30}; do
  if docker exec horizon-test-postgres pg_isready -U postgres -d horizon_test >/dev/null 2>&1 \
    && curl -fsS --max-time 2 http://localhost:59000/minio/health/live >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[[ $ready == 1 ]] || { echo "test infrastructure did not become ready" >&2; false; }
```

Provision the bucket and the restricted ingestion account the tests expect (`test-ingestion` /
`disposable-ingestion-password`). CI runs the compiled server and client directly on `localhost`.
For a client container on Docker Desktop (macOS, Windows), use `host.docker.internal` as the host name:

```zsh
MINIO_ADMIN_HOST=host.docker.internal
docker run --rm --entrypoint /bin/sh \
  -v "$PWD/dev/stack/minio:/provision:ro" \
  -e MC_HOST_local="http://test-admin:disposable-test-password@${MINIO_ADMIN_HOST}:59000" \
  -e INGESTION_MINIO_ACCESS_KEY=test-ingestion \
  -e INGESTION_MINIO_SECRET_KEY=disposable-ingestion-password \
  quay.io/minio/mc:RELEASE.2025-04-16T18-13-26Z /provision/provision.sh
```

Expected: `mc` creates the versioned `horizon-documents` bucket, the `test-ingestion` user and policy, and
exits 0.

### Run

```zsh
export TEST_DATABASE_DSN=postgresql://postgres:test@localhost:55432/horizon_test
export TEST_MINIO_ENDPOINT=http://localhost:59000
export REQUIRE_INTEGRATION=1
uv run --locked pytest -m integration
```

Narrow selections: `services/chat/tests/integration` (database only, no MinIO),
`services/ingestion/tests/integration` (database and MinIO), `services/migrations/tests/integration`.
Ingestion tests write under a unique `attempts/<uuid>/` prefix in the bucket; each test's database is
dropped afterwards, and removing the MinIO container (below) discards any remaining objects.

The database-backed suites need **no AWS access**: chat and ingestion integration tests use scripted
models and fake embedding vectors.

### Migration job container variants

Some `test_job.py` cases run the real migration image when `MIGRATION_IMAGE` is set (otherwise they skip):

```zsh
docker build -f services/migrations/Dockerfile -t horizon-migrations:local .
export MIGRATION_IMAGE=horizon-migrations:local
# Docker Desktop reaches the host as host.docker.internal (the default);
# on Linux CI the host is 127.0.0.1:
# export DOCKER_DATABASE_HOST=127.0.0.1
```

### Clean up

```zsh
docker rm -f horizon-test-postgres horizon-test-minio
unset TEST_DATABASE_DSN TEST_MINIO_ENDPOINT REQUIRE_INTEGRATION MIGRATION_IMAGE
```

### Test inputs

| Variable | Read by | Meaning |
|---|---|---|
| `TEST_DATABASE_DSN` | root `conftest.py` | Admin DSN to a `*_test` PostgreSQL database |
| `TEST_MINIO_ENDPOINT` | ingestion `conftest.py` | Disposable MinIO; account `test-ingestion` / `disposable-ingestion-password`, provisioned by `provision.sh` |
| `REQUIRE_INTEGRATION` | root `conftest.py` and ingestion `conftest.py` | `1` fails instead of skipping a missing database or MinIO; it does not convert the skips of the migration container tests when `MIGRATION_IMAGE` is unset |
| `MIGRATION_IMAGE` | `services/migrations/tests/integration/test_job.py` | Image to run for the container variants |
| `DOCKER_DATABASE_HOST` | same | Host name the container uses to reach PostgreSQL (default `host.docker.internal`) |

## What the tests rely on

- **Test doubles** live in [`services/chat/tests/horizon_chat_testing`](../../services/chat/tests/horizon_chat_testing)
  and [`services/ingestion/tests/horizon_ingestion_testing`](../../services/ingestion/tests/horizon_ingestion_testing):
  a scripted chat model that replays messages or exceptions, a controlled agent for stream tests, a recording
  retrieval index, and an in-process ingestion harness running the real app over real PostgreSQL, MinIO and
  parsers with fake embeddings.
- **Fitness tests** guard conventions: `test_db_vocabulary.py` (database code never writes literal status
  strings), `test_api_errors.py` (every request error class is mapped), settings tests (shutdown grace fits
  Compose's stop grace; the worker `stop_grace_seconds` mirrors `compose.yaml`).
- The ingestion operator query `-- stuck-deletions` in `services/ingestion/README.md` is executed by an
  integration test.
- `dev/stack/smoke.py` is a manual OpenTelemetry canary CLI, not a pytest test
  ([Observability](../operations/observability.md#verifying-the-pipeline)).

## Frontend

From `services/frontend` (Node 24):

```zsh
cd services/frontend
npm ci
npm run lint
npm run typecheck
npm test
npx playwright install chromium
npm run test:browser
```

`npm run check` chains lint, build (which type-checks), unit tests and browser tests. Vitest runs only
`src/**/*.test.ts` in a plain Node environment. The Playwright suite builds and serves production assets
and fulfils every network call from fixtures: it stubs Google Identity Services, the fonts, `config.js`
and both APIs, so it uses no real tokens and calls no AWS. It includes a real local HTTP stream to verify
incremental rendering, axe accessibility checks, a 390 px viewport and reduced-motion scenarios.

**Port collision.** Playwright serves on `FRONTEND_TEST_PORT` (default **3002**) and never reuses an
existing server. The Compose stack publishes Langfuse on 3002 by default, so with the local stack running
use another port: `FRONTEND_TEST_PORT=3003 npm run test:browser`.

Browser mocks cannot prove durable database joins or real Google and AWS behaviour; the backend integration
suites and the [smoke test](smoke-test.md) cover those layers. Vitest also writes JUnit XML in CI.

## CI

Workflows in [`.github/workflows`](../../.github/workflows) run on every push, pull request and manual
dispatch, cancel superseded runs, pin actions to commit SHAs and use read-only repository access.

Both Python jobs install the interpreter from `.python-version` using `actions/setup-python` before
setting up uv. The integration job builds MinIO and `mc` from the upstream commits for
`RELEASE.2025-04-22T22-12-26Z` and `RELEASE.2025-04-16T18-13-26Z`, using their Go module checksums.
Source checkouts under `.ci/` are excluded from Git and Docker build contexts. The server runs on
`127.0.0.1:9000` and uses the same bucket versioning and restricted-user provisioning script as the
local stack.

| Job (required-check name) | Runs |
|---|---|
| `Python quality` | `uv sync --locked --all-packages`, `scripts/quality.sh --junitxml=…` |
| `Python integration` | pgvector PostgreSQL service + provisioned MinIO + migrations image, then `pytest -m "not live"` with coverage XML and JUnit |
| `Frontend quality` | `npm ci`, lint, build, `npm test` with JUnit |
| `Frontend browser` | `npx playwright install --with-deps chromium`, `npm run test:browser` (2 workers, `--forbid-only`) |
| `Docker build (chat)`, `(ingestion)`, `(migrations)`, `(frontend)` | `docker build` of each image (not pushed) |

Reports (JUnit, coverage, Playwright HTML with traces and screenshots on failure) are uploaded as artifacts for
14 days. To open a downloaded browser report, run `npx playwright show-report <report-directory>` from
`services/frontend`. Coverage has no minimum gate and does not measure `horizon_config`.

Branch protection requiring these eight checks is a GitHub repository setting; workflow files alone do not
enforce merge protection, and it is **not yet configured**. When it is, require pull requests and all eight
checks, and keep the job names above stable when editing the workflows. The open activation steps are
tracked in [`TODO.md`](../../TODO.md#github-ci-activation).
