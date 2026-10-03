# System architecture

Horizon Copilot is a document-grounded assistant: users sign in with Google, upload project documents
(PDF or Word), and ask questions that an agent answers with cited excerpts from those documents. This
page shows the components, who owns which state, how they communicate, and where trust boundaries sit.
Concrete end-to-end flows are in [Request lifecycle](request-lifecycle.md).

## Diagram

[![Horizon Copilot system architecture: browser, React frontend, Google identity, chat API, ingestion API and worker, PostgreSQL with pgvector, MinIO and AWS Bedrock](horizon-copilot-arch-diagram.png)](horizon-copilot-arch-diagram.png)

*Canonical diagram, supplied with the repository ([full size](horizon-copilot-arch-diagram.png)).*
Reading it: the user's browser loads the **frontend** (static React/Vite app on port 3000) and signs in
with **Google Identity**. The frontend calls the **chat API** (REST and SSE, port 8080) and the
**ingestion API** (upload and status, port 8081) directly. The chat API runs the LangGraph agent, queries
**PostgreSQL** (conversation state and vector search) and calls **AWS Bedrock** for inference and
embeddings. The ingestion API stores originals in **MinIO** and records documents and jobs in PostgreSQL;
the **ingestion worker** claims jobs, reads originals from MinIO, embeds chunks with Titan through Bedrock
and writes vectors back to PostgreSQL. The dashed box, labelled "Horizon backend (Docker Compose)", encloses the chat API, ingestion API, worker, PostgreSQL and MinIO; the frontend is drawn outside it although Compose runs it too.

The diagram shows the runtime request path only. It does not show:

- the one-shot **migration job** and runtime-grant step that prepare PostgreSQL ([Migration job](../services/migrations.md));
- the **observability stack** (Collector, Grafana bundle, Langfuse, Alloy), which Compose also runs
  ([Observability](../operations/observability.md));
- the HTTPS call each API makes to **Google** to fetch signing certificates when verifying a token
  ([Integrations](../reference/integrations.md#google-identity));
- the Compose-only database and storage setup containers.

Nothing in the diagram conflicts with the code; the above are omissions.

## Components

| Component | Kind | Port (local) | Responsibility | State it owns | Docs |
|---|---|---|---|---|---|
| Frontend | Static SPA behind nginx | 3000 | Sign-in, chat UI, document management | None (token in memory only) | [Frontend](../services/frontend.md) |
| Chat API | FastAPI, one process | 8080 | Conversations, streaming turns, agent, retention | Chat tables, LangGraph checkpoints | [Chat](../services/chat.md) |
| Ingestion API | FastAPI, one process | 8081 | Accept uploads, report status, retry, delete | Writes document/job tables | [Ingestion](../services/ingestion.md) |
| Ingestion worker | Always-on process, same image | none (health probe only) | Extract, chunk, embed, publish, clean up | Writes chunks, vectors, job state; deletes objects | [Ingestion](../services/ingestion.md) |
| Migration job | One-shot container | none | Application and checkpoint schema | Schema `app` and `langgraph` DDL | [Migration job](../services/migrations.md) |
| PostgreSQL + pgvector | Database | 5432 (127.0.0.1) | Durable state, vector search, job queue | Everything above | [Data model](../reference/data-model.md) |
| MinIO | Object store | 9000, console 9001 | Versioned original files | `horizon-documents` bucket | [Ingestion](../services/ingestion.md) |
| AWS Bedrock | External | n/a | Chat models, Titan embeddings | n/a | [Integrations](../reference/integrations.md) |
| Google | External | n/a | Identity (ID tokens, signing certificates) | n/a | [Authentication](../guides/authentication.md) |
| Telemetry stack | Collector, Grafana bundle, Langfuse, Alloy | 4318, 13133, 3001, 3002 | Local traces, metrics, logs | Local volumes | [Observability](../operations/observability.md) |

Shared Python libraries (`libs/*`) are packaged into the images, not deployed:
[Shared libraries](shared-libraries.md).

## State ownership

- **Chat owns conversation state**: users' conversations, turns, messages, runs, feedback and LangGraph
  checkpoints. Only the chat role can write them; the ingestion role cannot read them.
- **Ingestion owns document state**: documents, versions, chunks, jobs, upload ledger, vendor permits,
  and the original files in MinIO. Chat reads documents, versions and chunks with `SELECT` only and
  has no object-store access.
- **`users`** is created lazily by whichever service sees a subject first; both resolve ownership by
  joining on `users.subject`.
- **The migration job owns schema.** Runtime roles hold DML privileges only.
- **The frontend owns nothing durable.** Reloading the page signs the user out.

## Communication

| Source | Destination | Interface | Sync/async | Purpose | Failure behaviour |
|---|---|---|---|---|---|
| Browser | Frontend | HTTP(S) static files | sync | Load the SPA and `config.js` | nginx serves from the image; `/health` for probes |
| Frontend | Google | Google Identity Services script | sync | Obtain an ID token | Sign-in screen cannot proceed |
| Frontend | Chat API | REST + SSE with bearer token | sync, streamed | Conversations, turns, feedback | Client reconciles unknown outcomes from saved state |
| Frontend | Ingestion API | REST (multipart upload) with bearer token | sync, then polled | Upload, status, retry, delete | Idempotent retries; status polling with backoff |
| Chat API / Ingestion API | Google | HTTPS GET of signing certificates | sync, per verification | Verify the bearer token | 503 (not 401) when unreachable |
| Chat API | PostgreSQL | SQL (SQLAlchemy async, psycopg pool) | sync | State, retrieval, checkpoints | Outage → 503 / `service_unavailable`; startup fails if unreachable |
| Chat API | Bedrock | Converse / ConverseStream, `InvokeModel` | sync, streamed | Agent inference, query embedding | Bounded retries; maps to failure categories |
| Ingestion API | MinIO | S3 `PutObject`, `HeadObject` | sync | Store original | 503; orphans reconciled later |
| Ingestion API | PostgreSQL | SQL + `NOTIFY ingestion_jobs` | sync | Record document and job | 503; idempotent retry |
| Ingestion worker | PostgreSQL | SQL claims, `LISTEN ingestion_jobs` | async (queue) | Claim jobs, write results | Leases and fencing; periodic scan recovers lost notifications |
| Ingestion worker | MinIO | S3 `GetObject`, `DeleteObject`, `ListObjectVersions` | sync | Read and clean up originals | Retried by job policy |
| Ingestion worker | Bedrock | `InvokeModel` (Titan) | sync | Embed chunks | Per-call, per-chunk and per-job retry budgets |
| Services | Collector | OTLP/HTTP | async, best effort | Traces and metrics | Dropped silently; never affects requests |
| Containers | Alloy → Loki | Docker log stream | async | Logs | Local only |

## Trust boundaries and security model

**Authentication.** Both APIs require a Google **ID token** whose audience is the configured client ID
(`Authorization: Bearer …`). Verification checks signature, issuer, audience and expiry and takes the
owner key from `sub`. Access tokens and API keys are not accepted. There is no machine-to-machine
identity between services: the APIs do not call each other. A local-development mode accepts any request
as one fixed user, and is refused unless the environment is `local` and the bind address is loopback
([Authentication](../guides/authentication.md)).

**Authorization.** There is no role model, email allowlist or admin tier. Authorization is **ownership**:
every query is scoped to the caller's `users.subject`, enforced in SQL (retrieval filters `visibility = 'shared' OR owner = caller` before ranking; no code sets `shared`, so today a caller matches only their own documents). Cross-owner access returns 404. Any Google account with a valid token for the client ID can use the system and sees only its own data. Documents are private to the uploader unless `visibility` is changed by hand in SQL.

**Credential sources.**

| Credential | Held by | Notes |
|---|---|---|
| Google ID token | Browser memory | Never persisted; no OAuth client secret exists |
| AWS credentials | Chat and ingestion environment (or the SDK chain) | Shared `.env` values in local Compose |
| Database credentials | Per-role logins: chat, ingestion, two migrators | Distinct roles; runtime roles cannot run DDL |
| MinIO credentials | Ingestion only | Policy limited to `attempts/*` plus bucket reads |
| Langfuse project keys | Collector | Local defaults in Compose |

**Network exposure (local Compose).** Every published port binds `127.0.0.1`. The APIs listen on
`0.0.0.0` *inside* their containers, which the settings validators allow only because identity mode is
`google`. Grafana allows anonymous admin, and Langfuse, MinIO and databases use shared development
credentials; none of this is suitable beyond a developer machine. Production network design, TLS
termination, WAF and rate limiting are **not defined** in this repository. Both APIs leave their
auto-generated `/docs`, `/redoc` and `/openapi.json` pages enabled and unauthenticated.

**Sensitive data.** Prompts, answers, document text, tokens and exception messages are not written to
logs, spans or metrics; log and telemetry fields are allowlisted
([Observability](../operations/observability.md)). The full conversation text *is* stored in PostgreSQL
until retention deletes it, and original files stay in MinIO until the document is deleted.

**Policy versus implementation.** Statements such as "owner-isolated" and "content-free telemetry" are
enforced by SQL joins, role grants, allowlists and tests named in the linked pages. "Secure" is not
claimed beyond those mechanisms.

## Concurrency, scaling and limits

- **One active turn per conversation**, enforced by a database lease; different conversations run
  concurrently. There is no per-user or global limit on turns, uploads or request rate.
- **Chat** is a single uvicorn process; Bedrock calls are synchronous clients on a thread pool.
  Running several replicas is not documented or tested ([Chat](../services/chat.md#deployment-and-scaling)).
- **Ingestion** scales by adding worker processes: claims use `FOR UPDATE SKIP LOCKED` with generation
  fencing, and embedding concurrency is capped globally (default 4) by database permits.
- **Throughput limits** are the Bedrock quota of the account, the permit cap, a 4-job-per-worker default,
  and sequential per-document embedding.
- **PostgreSQL** is the single stateful dependency for both services; a database outage stops new turns,
  uploads and job progress but is survivable (leases expire, intents are replayed, jobs retry).

## Where architectural constraints are enforced

| Constraint | Enforced by |
|---|---|
| Layering and library boundaries inside each Python deployable | 25 import-linter contracts in [`pyproject.toml`](../../pyproject.toml), run by `scripts/quality.sh` |
| Runtime services never run DDL or import migrations | The import-linter contract "runtime services do not run migrations" plus role privileges |
| Chat cannot write documents; ingestion cannot read conversations | `runtime_grants.sql`, asserted by integration tests and `verify-roles.sql` |
| Embedding model and dimension agree across services | `libs/genai`, settings (`Literal[1024]`), `vector(1024)` column, readiness checks |
| Safe settings combinations | Pydantic validators in each `Settings` class |
| Status vocabularies | Database `CHECK` constraints and a fitness test forbidding literal status strings in SQL code |

## Local versus deployed

Local Compose runs everything on one machine with development defaults. Beyond it the repository
defines the container images, the provisioning/migration/grant order and `staging` and `production`
settings layers (currently empty), but no infrastructure code, pipeline, registry, environments or
runbooks ([Deployment](../guides/deployment.md)).
