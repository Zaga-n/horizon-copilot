# Local deployment

Run the whole system (database, object store, both APIs, the ingestion worker, the frontend and the
observability stack) on one machine with Docker Compose. Everything is defined in
[`compose.yaml`](../../compose.yaml); credentials in it are shared **local-development** defaults.

Run all commands from the repository root.

## Prerequisites

| Need | Detail |
|---|---|
| Docker with Compose v2.20 or newer, Linux containers | Compose defines 19 services (15 long-running plus 4 one-shot setup jobs). The runbook suggests roughly 6 GB RAM for the core stack and 12 GB / 4 CPUs with Langfuse; these are starting allocations, not measured minimums |
| A public Google OAuth web client ID | With `http://localhost:3000` in **Authorized JavaScript origins** ([Authentication](authentication.md#one-time-google-setup)). Compose refuses to start without `GOOGLE_CLIENT_ID` |
| AWS credentials and Bedrock model access | For the chat and embedding models in `AWS_REGION`; model calls incur your account's charges. The stack becomes healthy without them, but chat answers and document indexing need them |
| uv and Node | Only for running tests or the dev server on the host; not needed for the Compose stack |

## 1. Configure

```zsh
cp .env.example .env
```

Edit `.env` (it is git-ignored) and set at least:

- `GOOGLE_CLIENT_ID`: the public client ID.
- `MAIN_MODEL_ID`, `UTILITY_MODEL_ID`: Bedrock inference profiles available to your account.
- `AWS_REGION`, and `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN` for temporary
  credentials; all three are required together in that case). The same values serve chat and ingestion.

Database, MinIO and Langfuse credentials have fixed local defaults; there is nothing to fill in. All
variables: [Configuration → Compose inputs](../reference/configuration.md#compose-inputs). To use an SDK
login profile instead of static keys, leave the AWS key variables empty and add a Compose override that
mounts the profile into `/home/appuser/.aws` and sets `AWS_PROFILE`; Compose does not do this itself.

Validate the file without starting anything. It fails only when a required variable is unset or empty, so it passes with the placeholder values copied from `.env.example`; replace those, and clear the `replace-me` AWS keys if you intend to use an SDK profile:

```zsh
docker compose config --quiet
```

## 2. Start

```zsh
docker compose up --build -d
```

Expected: images build; the one-shot containers `minio-setup`, `migrations`, `runtime-grants` and
`langfuse-storage-setup` run and exit with code 0; the long-running services become healthy. Startup
order is: PostgreSQL (its first-start script creates the roles and passwords) and MinIO →
`minio-setup` (creates the private, versioned `horizon-documents` bucket and the restricted ingestion
account) and `migrations` (application schema, then checkpoint schema) → `runtime-grants` →
`chat`, `ingestion`, `ingestion-worker`. The frontend has no dependencies and may load before the APIs
are ready.

Wait for readiness with a bounded loop (the first build can take several minutes):

```zsh
ready=0
for attempt in {1..60}; do
  if curl -fsS --max-time 3 http://localhost:8080/ready >/dev/null 2>&1 \
    && curl -fsS --max-time 3 http://localhost:8081/ready >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 5
done
[[ $ready == 1 ]] || { echo "APIs not ready: run 'docker compose ps -a' and 'docker compose logs migrations'" >&2; false; }
```

Then check the rest:

```zsh
docker compose ps -a
curl -fsS http://localhost:13133/     # Collector health
```

`/ready` returning `{"status":"ready"}` means PostgreSQL answers and the schema revision matches (chat additionally checks the checkpoint schema, the vector column type and chunk compatibility; ingestion checks the revision and configured dimension). It succeeds with an empty index and does **not** call AWS or Google. It does not mean a chat turn will succeed.

If startup stalls or a service is unhealthy, see [Troubleshooting](../operations/troubleshooting.md).
A failed migration blocks every runtime: read `docker compose logs migrations`, fix the cause, then run
`docker compose up -d` again.

### Optional checks

Verify the database role boundaries (writes roll back):

```zsh
docker compose exec -T postgres psql -U postgres -d horizon < dev/stack/postgres/verify-roles.sql
```

Repeat the migration job (safe; only the job, not the grants):

```zsh
docker compose run --rm migrations
```

Running only part of the stack to save memory (for example the application path without Langfuse and
Grafana) should work because no application service depends on the telemetry services, but this was not
exercised. Telemetry export then fails silently.

## 3. Use it

| What | Where |
|---|---|
| Frontend | http://localhost:3000 |
| Chat API | http://localhost:8080 (`/ready`, `/v1/…`) |
| Ingestion API | http://localhost:8081 (`/ready`, `/v1/…`) |
| Grafana (anonymous admin) | http://localhost:3001 (`GRAFANA_PORT`) |
| Langfuse | http://localhost:3002 (`LANGFUSE_PORT`; seeded account from `LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD`) |
| MinIO console | http://localhost:9001 (`MINIO_CONSOLE_PORT`; `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) |
| PostgreSQL | `127.0.0.1:5432` (`POSTGRES_PORT`) |
| MinIO API / Collector OTLP | 9000 / 4318 |

All published ports bind `127.0.0.1`. The API and frontend host ports are fixed in `compose.yaml`.

To exercise a full business flow you need, in order: a Google sign-in that matches the authorized origin;
valid AWS credentials with model access; at least one indexed document (an empty index is valid, but
project-specific answers then disclose missing evidence). A sample file is provided at
[`dev/data/9.2-Project-management-Handbook.pdf`](../../dev/data/9.2-Project-management-Handbook.pdf).
The step-by-step check is the [Smoke test](smoke-test.md).

For token-free backend work, run the APIs on the host in local identity mode
([Authentication](authentication.md#local-identity-mode)).

## Stop, restart and inspect

| Goal | Command | Effect |
|---|---|---|
| Restart the applications | `docker compose restart chat ingestion ingestion-worker` | No DDL. Accepted ingestion jobs are rediscovered without re-uploading |
| Stop and remove containers, keep data | `docker compose down` | Volumes (`postgres-data`, `minio-data`, telemetry, Langfuse) remain |
| Start again / repeat ordered setup | `docker compose up -d` | One-shot setup steps run again safely |
| Logs | `docker compose logs -f chat` (or `ingestion`, `ingestion-worker`, `migrations`) | JSON logs for the applications |
| Worker health | `docker compose exec ingestion-worker python -m horizon_ingestion.main worker-health` | Exit 0 if healthy |

Backend health never depends on the observability UIs.

## Reset

**Destructive: deletes every volume of this Compose project** (database, original files, telemetry,
Langfuse). Take a backup first ([Recovery](../operations/recovery.md#local-backup-and-restore)).

```zsh
docker compose down --volumes
docker compose up --build -d
```

Never reset an existing project to fix a configuration error. Changing a password variable in `.env` does
**not** rotate accounts already stored in an existing volume (for example the PostgreSQL roles, which
are created and given passwords only when the data volume is first initialised, and the seeded Langfuse
account); rotate them explicitly, or back up and reset disposable volumes. Do not change `LANGFUSE_ENCRYPTION_KEY` for an existing Langfuse volume. Volumes from an
earlier standalone Grafana/Tempo/Loki/Mimir deployment are neither migrated nor removed.

## Local limits worth knowing

- Chat and ingestion run with `IDENTITY_MODE=google` in Compose, so the APIs need real ID tokens whose
  audience equals `GOOGLE_CLIENT_ID`.
- Changing the host ports of the APIs or frontend means editing `compose.yaml`, the APIs' allowed origin
  and the Google authorized origin together.
- Chat retention runs daily in Compose (`MAINTENANCE_INTERVAL_SECONDS=86400`), not hourly as in the YAML.
