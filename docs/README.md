# Horizon Copilot documentation

The engineering manual for Horizon Copilot, organised by what you want to do. New here? Read
[Overview](overview.md), then [System architecture](architecture/system.md), then
[Local deployment](guides/local-deployment.md).

## Understand the system

| Page | Answers |
|---|---|
| [Overview](overview.md) | What it does, who uses it, glossary of terms, scope and limits |
| [System architecture](architecture/system.md) | Components, state ownership, communication, trust boundaries, scaling; embeds the architecture diagram |
| [Request lifecycle](architecture/request-lifecycle.md) | How a question and an upload travel through the system, and what happens on failure |
| [Shared libraries](architecture/shared-libraries.md) | What lives in `libs/` and the rules that keep it small |

## Components

| Page | Component |
|---|---|
| [Chat service](services/chat.md) | Conversations, streaming turns, the Horizon agent, retrieval, retention |
| [Ingestion service](services/ingestion.md) | Upload API and indexing worker: acceptance, extraction, chunking, embedding, deletion |
| [Migration job](services/migrations.md) | Schema migrations, checkpoint setup, provisioning order |
| [Frontend](services/frontend.md) | React/Vite SPA, runtime config, session handling, nginx image |

## Run and use

| Page | Task |
|---|---|
| [Local deployment](guides/local-deployment.md) | Start, check, stop, back up and reset the Docker Compose stack |
| [Authentication](guides/authentication.md) | Google ID tokens, getting a token for testing, local identity mode |
| [Smoke test](guides/smoke-test.md) | Verify a running system: readiness, an API walk-through, the browser workflow |
| [API reference](reference/api.md) | Chat and ingestion HTTP contracts, errors, health |
| [Events and queues](reference/events.md) | The chat SSE stream and the ingestion job queue |

## Develop and test

| Page | Task |
|---|---|
| [Development](guides/development.md) | Toolchain, repository map, enforced rules, change recipes |
| [Testing](guides/testing.md) | Quality gate, unit, integration and frontend tests, CI jobs |
| [Configuration reference](reference/configuration.md) | Every setting, precedence, Compose inputs |
| [Data model](reference/data-model.md) | Tables, roles and privileges, retention and deletion |
| [Integrations](reference/integrations.md) | Bedrock, Google, MinIO, PostgreSQL: operations, auth, failure handling |

## Deploy and release

| Page | Task |
|---|---|
| [Deployment](guides/deployment.md) | First deployment, repeat deployment, rollback, platform prerequisites |

## Operate

| Page | Task |
|---|---|
| [Observability](operations/observability.md) | Metrics, traces, logs, the Collector pipeline, dashboards, correlating a request |
| [Troubleshooting](operations/troubleshooting.md) | Symptom-driven diagnosis |
| [Recovery](operations/recovery.md) | Automatic recovery, safe operator actions, backup and reset |

## Component notes kept next to the code

These pre-date this manual and are supplementary. Where they disagree with the pages above, trust the
pages above (see the first gap below).

- [`dev/stack/README.md`](../dev/stack/README.md): the original local-stack runbook.
- [`services/ingestion/README.md`](../services/ingestion/README.md): contains the operator query
  `-- stuck-deletions`, which an integration test executes (so it is the canonical copy).
- [`services/frontend/README.md`](../services/frontend/README.md), and the READMEs of
  [`libs/genai`](../libs/genai/README.md), [`libs/google-identity`](../libs/google-identity/README.md) and
  [`libs/observability`](../libs/observability/README.md).
- Change records: [`openspec/changes/`](../openspec/changes). Open items: [`TODO.md`](../TODO.md).

## Known gaps

- **Component READMEs are partly stale.** They mention `CHAT_PORT`, `INGESTION_PORT` and `FRONTEND_PORT`,
  which `compose.yaml` does not read (those ports are fixed); the ingestion README says the telemetry stack
  is a separate change (it ships in the repository) and says online Bedrock access is not verified while `dev/stack/verification.md` records a live run. See [Configuration](reference/configuration.md#compose-inputs).
- **No production deployment definition.** No infrastructure code, pipeline, registry, environments,
  secrets management or ingress, and it is not defined how production database logins assume the
  `NOLOGIN` runtime roles. See [Deployment → Known gaps](guides/deployment.md#known-gaps).
- **No rolling-update or rollback procedure.** Readiness requires an exact schema-revision match in both
  directions and migrations cannot be downgraded. See [Deployment](guides/deployment.md#repeat-deployment-release).
- **No alerting and no production telemetry destination**, and the Collector's attribute allowlist drops
  retrieval and usage attributes the services emit. See [Observability](operations/observability.md#known-gaps).
- **No restore procedure and no production backup definition.** See [Recovery](operations/recovery.md#local-backup-and-restore).
- **No automated live or signed-in test.** Bedrock access and real Google sign-in are only checked manually.
  See [Smoke test](guides/smoke-test.md).
