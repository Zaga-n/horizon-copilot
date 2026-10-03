# Local backend and observability

Run commands from the repository root. Compose needs Docker Compose v2.20+,
Docker Linux containers, and approximately 6 GB available RAM for the core stack;
allow 12 GB and 4 CPUs with Langfuse. These are starting allocations, not measured
minimums. Persistent volumes hold PostgreSQL, originals, dashboards, logs, traces,
and metrics. No frontend is required. All published ports bind to `127.0.0.1`.

## Configure and start

Copy `.env.example` to `.env` and set the Google audience and AWS/model inputs.
Database, MinIO, and Langfuse credentials have fixed local development defaults
in Compose; there is no infrastructure password inventory to fill in. Grafana
allows anonymous access. These defaults are for local development. Override the
corresponding Compose variables in the ignored `.env` if needed. Changing a
password variable does not rotate accounts in existing volumes; rotate explicitly
or back up and reset disposable volumes.

Set your public Google OAuth client ID. Both APIs expect a Google **ID token**
whose audience is that client ID in `Authorization: Bearer <id-token>`; an access
token or developer API key is not accepted. Obtain an ID token with a Google OAuth
flow configured for that audience. No OAuth client secret belongs in either API.
The client ID is the OAuth application audience, not a user ID or user allowlist.
The backend checks that Google issued the ID token for our frontend application;
it also verifies signature and expiry. Any user with a valid token for that
audience can authenticate under the current contract. The verified Google `sub`
owns data, so use the same identity for upload and chat.

Set valid AWS credentials with access to the configured Bedrock inference profiles
and Titan V2 embedding model in `AWS_REGION`. Temporary credentials require all
three AWS variables. MinIO credentials are independent of AWS credentials.
For the SDK credential chain instead, leave explicit AWS variables empty and
mount an authorized profile into the application's `/home/appuser/.aws`, with the
appropriate profile environment setting. Model calls incur your account's usage.
Readiness does not call AWS and succeeds with an empty index.

```sh
docker compose config --quiet
docker compose up --build -d
docker compose ps -a
curl -fsS http://localhost:8080/ready
curl -fsS http://localhost:8081/ready
curl -fsS http://localhost:13133/
```

Startup order is PostgreSQL/bootstrap roles and healthy MinIO → migration job
(`app` Alembic followed by `langgraph` setup) → runtime grants → APIs and worker.
MinIO setup creates the private, versioned `horizon-documents` bucket and a
restricted ingestion account with access only to `attempts/*`. No S3 event hook
or presigned upload route is provisioned. A failed migration blocks all runtimes.
Inspect it with `docker compose logs migrations`; after fixing the cause, run
`docker compose up -d` again. Repeating the migration job is safe:

```sh
docker compose run --rm migrations
docker compose exec -T postgres psql -U postgres -d horizon < dev/stack/postgres/verify-roles.sql
```

PostgreSQL uses distinct authenticated schema owners and runtime roles. Chat has
read-only document access and checkpoint DML. Ingestion cannot read chat history
or access checkpoints. Runtime search paths include `public` for pgvector types
and operators; public schema CREATE remains revoked.

UIs: Grafana http://localhost:3001 (anonymous), Langfuse http://localhost:3002
(`admin@admin.com` / `admin` by default), MinIO
console http://localhost:9001 (`horizon-admin` / `horizon-local-minio-admin`). API ports are 8080/8081,
PostgreSQL 5432, object storage 9000, Collector HTTP OTLP 4318. Optional host port
variables are `GRAFANA_PORT`, `CHAT_PORT`, `INGESTION_PORT`, `POSTGRES_PORT`,
`MINIO_PORT`, `MINIO_CONSOLE_PORT`, `OTLP_PORT`, and `COLLECTOR_HEALTH_PORT`.

Container APIs bind internally to `0.0.0.0` and explicitly use Google identity.
For token-free local identity, run the services on the host using their service
`.env.example` contracts with `IDENTITY_MODE=local` / `INGESTION_IDENTITY_MODE=local`
and `BIND_HOST=127.0.0.1` / `INGESTION_BIND_HOST=127.0.0.1`, while retaining the
Compose database, MinIO, and Collector. Stop the container APIs first to free
ports. Both services use `local-horizon-user`. The existing validation deliberately
requires loopback binding for local identity; a Docker port publication does not
make an internal public binding loopback.

## Worker and maintenance

The always-on worker uses a dedicated PostgreSQL LISTEN connection, subscribes
before its initial scan, and scans durable due jobs every two seconds. NOTIFY is
only a wakeup hint. Restart with `docker compose restart ingestion-worker`;
accepted jobs are rediscovered without uploading again. Listener reconnects use
bounded backoff. Job leases are 90s with 20s heartbeats; vendor permits expire
after 45s. Physical calls, chunks, and jobs have bounded retry counts (3/9/3).
Orphan scans run every 60s with a 600s grace period; deletion removes exact object
versions through durable cleanup. These defaults live in `config/services/ingestion.yaml`.
The worker uses its own supervision, separate from API readiness. Its container
healthcheck runs `python -m horizon_ingestion.main worker-health`, reporting
listener connection state and the age of the last successful recovery scan.
Recent scans keep it healthy if LISTEN is temporarily disconnected; a missing
or stale scan, a required loop crash, or a stale health snapshot fails the probe.
The process-local snapshot is atomically replaced and removed during shutdown.
Inspect it with:

```sh
docker compose exec ingestion-worker python -m horizon_ingestion.main worker-health
```

The parser child has a finite 1 GiB virtual-memory limit. Linux spawn imports
the service before parsing, so its imported virtual address space already
exceeds 512 MiB. The larger limit allows valid DOCX/PDF parsing while retaining
the existing time, expanded-size, unit and output limits.

Chat runs maintenance daily with 30-day inactivity retention, persisted lease
coordination, and startup catch-up. Active turns are protected. Conversations,
messages, feedback, and checkpoints expire together; users, documents, original
objects, chunks, and ingestion state do not expire with chat history. Restarting
chat catches up a missed daily maintenance run. An empty index is valid: ask
general in-scope questions; project-specific answers must disclose absent evidence.

## Telemetry and Langfuse

Plain `docker compose up` starts the backends, Grafana LGTM bundle, and Langfuse.
No overlay, profile flag, or separate Langfuse environment template is required.
Applications push traces and metrics once to the routing Collector, which removes
private attributes and sends the complete traces plus metrics to the bundle's
OTLP endpoint. Its internal Collector forwards metrics to Prometheus's enabled
OTLP/HTTP receiver and traces to Tempo. No application scrape endpoint is added.
Collector self-metrics push directly to the bundle's Prometheus receiver, outside
the application routing pipeline. Alloy sends application container JSON logs
to the bundle's Loki. Grafana provisions all three datasources and the checked-in
chat/ingestion dashboards. Metrics keep bounded dimensions; execution IDs are
span metadata, not metric labels. Trace/log links preserve the original IDs.

The pinned `grafana/otel-lgtm:0.30.2` bundle persists its stores under `/data` in
`lgtm-data`. Prometheus retention is seven days. Grafana allows anonymous local
administration. Only its UI port is published; internal component ports are
accessible on the Compose network. The bundle's deployment reference is
https://github.com/grafana/docker-otel-lgtm/tree/v0.30.2 .

Langfuse v3.160.0 starts with separate PostgreSQL, Redis, ClickHouse, and MinIO
volumes. The assistant database retains its `app`/`langgraph` separation. Web
startup seeds a local project and development account. The routing Collector uses
HTTP Basic project-key auth via its basicauth extension; both web and Collector
use the same Compose `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` defaults or
overrides, so no manually calculated base64 variable is needed. Other Langfuse
password, salt, encryption, and login defaults can be overridden using their
`LANGFUSE_*` variables in `.env`. Do not change the encryption key for existing
encrypted data without following Langfuse's migration procedure.

Langfuse receives the same trace IDs and the known rooted GenAI ancestor tree,
without unrelated operational leaves. `app.conversation.id` maps to
`langfuse.session.id`; retries keep distinct run/trace IDs and attempt numbers.
The common redaction allowlist excludes credentials, content, events, and status
messages from both trace branches. The application has no second Langfuse callback
and does not export ratings. Langfuse is not a runtime readiness dependency; its
bounded in-memory export queue may lose data after retry exhaustion or Collector
restart. Use `docker compose stop langfuse-web langfuse-worker` to test an outage
and `docker compose up -d` to restore them.

Content capture is off. The existing flag alone cannot bypass the Collector
allowlist. An approved local capture experiment needs a separately reviewed
Langfuse content allowlist and approved/redacted app emission; this deployment
provides no tested capture-enabled configuration or reasoning/checkpoint dumps.

## Verify and inspect

For a fresh volume, verify roles, migration success, `/ready`, and bucket
versioning. For synthetic export checks without AWS or a Google token:

```sh
uv run --locked python dev/stack/smoke.py
uv run --locked python dev/stack/smoke.py --service horizon-ingestion
```

The JSON lines give conversation and trace IDs to inspect in Tempo/Langfuse.
The canary includes an operational sibling that must be absent from Langfuse and
fake private attributes that must be absent from both destinations. To exercise
Alloy's container stdout route too, run it in an opted-in container long enough
for discovery:

```sh
docker compose run --rm --no-deps -v "$PWD/dev/stack/smoke.py:/smoke.py:ro" ingestion \
  sh -c 'python /smoke.py --otlp http://collector:4318; sleep 15'
```

Synthetic telemetry verifies routing; it does not prove real model access or
application upload/chat behavior. Run the offline integration suite against a disposable database
ending in `_test` and disposable MinIO as documented in the service guides;
it uses controlled model boundaries and does not verify Bedrock access.

For live verification, use the direct upload example in `services/ingestion/README.md`,
poll the returned status URL through completion, and repeat the same idempotency
key after discarding the first response. The same job must return. Restart the
worker/listener while work is queued; it must complete without reupload. Force a
transient embedding failure, retry the failed job, and confirm completed chunks
are preserved. Delete the document and poll durable cleanup to `deleted`.

Create a chat conversation and use the streaming turn protocol in
[`docs/reference/events.md`](../../docs/reference/events.md#chat-turn-stream-sse).
Verify a cited answer with your indexed sample, then the empty-index behavior.
Restart runtimes and confirm persisted history. Exercise maintenance catch-up on
an expired conversation in a disposable volume. For one turn, compare the SSE
trace ID with Tempo and Langfuse; check every retained parent ID, exactly one model
span per physical request, linked Loki logs, and populated Prometheus dashboard panels.
Stop `langfuse-web` and `langfuse-worker`, repeat a turn, confirm `/ready` and
Grafana telemetry continue, and inspect Collector queue/retry self-metrics.

For a failed attempt followed by a successful rated retry, use:

```sh
docker compose exec -T postgres psql -U postgres -d horizon \
  -v conversation_id=<uuid> < dev/stack/postgres/history-join.sql
```

The user message and turn remain shared. Each run has its own assistant message,
attempt number and trace ID. The message feedback joins the rated assistant
message/run, not the earlier failed attempt. In Tempo search by trace ID; in
Langfuse use the same ID, with conversation session grouping. Loki can be searched
with `{service_name="horizon-chat"} | json | trace_id="<trace-id>"`.
Prometheus metrics retention is seven days; Tempo/Loki retention follows the
pinned bundle configuration, and Langfuse retention is managed independently. Missing traces may be unsampled, not exported, retry-exhausted,
expired, or lost with reset volumes. Stored history/feedback remains attributable
through its durable run/message keys. Do not replay model execution to recreate
an expired trace or copy the feedback into telemetry.

## Restart, backup, and reset

`docker compose restart chat ingestion ingestion-worker` restarts runtimes without
DDL. `docker compose down` removes containers/network and preserves volumes.
`docker compose up -d` repeats ordered setup safely. Backend health does not depend
on observability UIs. Langfuse account credentials are seeded once; update an existing account
explicitly when changing the initial user variables.

Before changing schemas or resetting, stop runtime writers and take a database
backup plus original objects (including versions):

```sh
mkdir -p dev/backups
docker compose stop chat ingestion ingestion-worker
docker compose exec -T postgres pg_dump -U postgres -d horizon -Fc > dev/backups/horizon.dump
```

Stop MinIO and archive its named volume to preserve exact object version IDs
and server metadata. A latest-object copy alone is insufficient to restore
PostgreSQL references to original versions.
For a consistent Langfuse backup, stop its web/worker processes and back up its
PostgreSQL, ClickHouse, Redis and MinIO volumes together. Verify restoration in a
separate Compose project. Dashboards are reproducible from checked-in JSON.

Explicit destructive reset of **this local project** (after backing up):

```sh
docker compose down --volumes
docker compose up --build -d
```

The root file includes the Langfuse volumes. Existing volumes from the previous
standalone Grafana/Tempo/Loki/Mimir deployment are not migrated or removed
automatically; `lgtm-data` starts a new telemetry store. Never reset an existing
project to solve a configuration error.
