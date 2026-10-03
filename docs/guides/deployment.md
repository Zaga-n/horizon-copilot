# Deployment

How to deploy Horizon outside the local Compose stack, for a first deployment and for repeat releases.

**Scope and honesty.** The repository defines container images, a fixed provisioning → migration → grants
→ start order, and `staging` / `production` settings layers (both currently empty, so they add nothing to
`base.yaml`). It does **not** contain infrastructure-as-code, a deployment pipeline, an image registry
workflow, environment definitions, secrets management, ingress/TLS, autoscaling or production runbooks.
CI builds the four images but never pushes or deploys them. This page therefore documents the supported
sequence and contracts, and lists what must come from your platform ([Known gaps](#known-gaps)). Nothing
here was executed against a remote environment.

## What you deploy

| Artifact | Build command (repository root) | Runs as |
|---|---|---|
| `horizon-migrations` | `docker build -f services/migrations/Dockerfile -t horizon-migrations:<tag> .` | One-shot job per release |
| `horizon-chat` | `docker build -f services/chat/Dockerfile -t horizon-chat:<tag> .` | One long-running API process, port 8080 |
| `horizon-ingestion` | `docker build -f services/ingestion/Dockerfile -t horizon-ingestion:<tag> .` | Two long-running processes from one image: API (`CMD`, port 8081) and worker (`python -m horizon_ingestion.main worker`) |
| `horizon-frontend` | `docker build -f services/frontend/Dockerfile -t horizon-frontend:<tag> .` | nginx serving static files, port 80 (or deploy `dist/` to any static host) |

All images are built with the repository root as context. Use an immutable tag (for example the commit
SHA) for each build; the schema revision compiled into the chat, ingestion and migrations images must be
the same for a given release.

## Platform prerequisites

- **PostgreSQL with pgvector.** The repository runs and tests PostgreSQL 17 with pgvector (Compose pins
  pgvector 0.8.2; CI uses `pgvector/pgvector:pg17`). Other versions were not exercised. An administrator
  must be able to create roles, schemas and the `vector` extension.
- **An S3-compatible bucket with versioning enabled** and a dedicated access key restricted as in
  [`dev/stack/minio/ingestion-policy.json`](../../dev/stack/minio/ingestion-policy.json); only MinIO
  provisioning ([`provision.sh`](../../dev/stack/minio/provision.sh)) is defined. The bucket must not be
  public. Uploads are refused if the store does not return object version IDs.
- **AWS Bedrock access** for the chat inference profiles and Titan text embeddings v2 in the configured
  region (IAM policy not defined here), or the supported static credentials.
- **A Google OAuth web client** whose authorized JavaScript origin is the frontend's HTTPS origin.
- **Network.** Both APIs need outbound HTTPS to Google (certificate fetch on token verification) and AWS;
  the browser must reach both API URLs; the ingestion worker needs the database, bucket and AWS.
  `production` requires an `https` frontend origin (settings validator; `staging` does not enforce it).
  A reverse proxy in front of chat must not buffer or compress the `POST …:stream` response.
- **Database logins.** The four roles are created `NOLOGIN`; your platform must provide authenticated
  logins that act as them (distinct for the two migrators and the two runtimes). The application never
  issues `SET ROLE`, and how logins assume roles is not defined here ([Known gaps](#known-gaps)).

## First deployment

Follow this order; each step depends on the previous one. Placeholders are in angle brackets.

1. **Provision PostgreSQL and the bucket.** Create the database and bucket; apply the bucket policy and
   enable versioning.
2. **Provision roles and schemas (administrator, once).** Run
   [`services/migrations/sql/provision.sql`](../../services/migrations/sql/provision.sql) as an administrator.
   It creates the `NOLOGIN` roles, the `vector` extension, schemas `app` and `langgraph`, the revoked-public
   defaults and the checkpoint helper function, and is safe to re-run. The pattern Compose uses for the
   grants file is a `psql` call with `ON_ERROR_STOP`:

   ```zsh
   psql "<admin-connection-string>" -v ON_ERROR_STOP=1 -f services/migrations/sql/provision.sql
   ```

   Then create the logins and role memberships your platform requires. The `provision.sql` file stores no
   passwords. Optionally revoke `CREATE` on the database from `PUBLIC` (the local script does; this one does
   not).
3. **Prepare configuration and secrets** (names only; values come from your secret store). Chat needs `ENVIRONMENT_NAME` and ingestion `INGESTION_ENVIRONMENT_NAME` (`staging` or `production`); the migration job needs neither. Decide the HTTPS frontend origin now: `FRONTEND_ORIGIN` / `INGESTION_FRONTEND_ORIGIN` is a required start-up setting of both APIs and is read once. The other required inputs are in
   [Configuration](../reference/configuration.md): chat `DATABASE_DSN`, `CHECKPOINT_DATABASE_DSN` (runtime
   login); ingestion `INGESTION_DATABASE_DSN`, `INGESTION_MINIO_*`; both `GOOGLE_CLIENT_ID` and AWS settings;
   migrations `MIGRATION_DATABASE_DSN`, `CHECKPOINT_MIGRATION_DSN`. Policy comes from the image's `config/`
   directory (`HORIZON_CONFIG_DIR=/app/config`); override values with environment variables, or mount a
   different directory. Production cannot select local identity.
4. **Run the migration job** with migration credentials only (no AWS or API settings) and require exit code 0. Export both DSNs in the invoking shell first (`-e NAME` forwards the existing value), and set `CHECKPOINT_MIGRATION_ROLE` if the checkpoint login's effective role is not `checkpoint_migrator`:

   ```zsh
   IMAGE_TAG='<immutable-tag>'   # the tag you built for this release
   docker run --rm \
     -e MIGRATION_DATABASE_DSN -e CHECKPOINT_MIGRATION_DSN \
     "horizon-migrations:$IMAGE_TAG"
   ```

   Optional: appending `sql` to that command prints the application DDL for review without connecting;
   appending `check` verifies the database is at head and matches the models. Expected
   output of `upgrade`: "Application and checkpoint migrations completed." On failure it exits 1 with a
   credential-free message: stop the deployment ([Migration job](../services/migrations.md)).
5. **Apply runtime grants (administrator).** Run
   [`services/migrations/sql/runtime_grants.sql`](../../services/migrations/sql/runtime_grants.sql) after
   the migration succeeds; it gives the runtime roles DML only and sets their role-level defaults:

   ```zsh
   psql "<admin-connection-string>" -v ON_ERROR_STOP=1 -f services/migrations/sql/runtime_grants.sql
   ```

   Optionally verify the role boundaries with
   [`dev/stack/postgres/verify-roles.sql`](../../dev/stack/postgres/verify-roles.sql) (its database name,
   `horizon`, is hard-coded; writes roll back).
6. **Start the runtime processes**: ingestion worker, ingestion API, chat API (any order). Give each
   ingestion worker replica a distinct `INGESTION_SERVICE_INSTANCE_ID`. Set the container stop grace of the
   worker to match `stop_grace_seconds` (75 s by default).
7. **Verify readiness.** `GET /ready` on both APIs must return `{"status":"ready"}`; the worker's health
   probe is `python -m horizon_ingestion.main worker-health`. A 503 usually means a revision mismatch (the
   image was built against a different schema revision than the database), a missing grant or a database
   problem ([Troubleshooting](../operations/troubleshooting.md)).
8. **Deploy the frontend** with `FRONTEND_GOOGLE_CLIENT_ID`, `FRONTEND_CHAT_API_URL` and `FRONTEND_INGESTION_API_URL` set to the **HTTPS, browser-reachable** URLs; confirm each API's `FRONTEND_ORIGIN` / `INGESTION_FRONTEND_ORIGIN` (step 3) equals the exact frontend origin (no trailing slash; a change needs an API restart) and add it to the Google client's authorized origins ([Frontend](../services/frontend.md#deploying-and-rolling-back)).
9. **Run the smoke test** against the deployed endpoints ([Smoke test](smoke-test.md)): readiness proves
   plumbing, not model access or a working business flow.

## Repeat deployment (release)

1. **Choose the version.** One immutable tag for all of chat, ingestion and migrations; frontend independently.
2. **Back up first when the release changes the schema** (database dump plus object versions;
   [Recovery](../operations/recovery.md)). There are no downgrades.
3. **Plan the cut-over, because readiness requires an exact schema match in both directions.** A runtime image
   is *unready* if the database revision is older **or newer** than its own. Consequently, after the migration
   job upgrades the database, every old replica reports 503 until replaced, and a new replica is unready
   until the migration has run. The repository defines no expand/contract or zero-downtime procedure:
   either accept a short unavailability window (stop old runtimes → run the migration job → apply updated
   grants → start new runtimes), or manage a rolling replacement yourself knowing old replicas drop out of
   rotation when the schema changes.
4. **Run the migration job** (step 4 above); it is safe at head. If the release adds tables, update and
   re-apply `runtime_grants.sql` (new tables have no automatic grants).
5. **Roll out runtimes**, then verify `/ready` and run the smoke test.
6. **Configuration-only changes** (environment or YAML policy) need no migration, only a restart, because
   settings are read once at start.
7. **Frontend** releases are independent but must follow API releases they depend on; keep previous hashed
   assets available during a rolling release.

Reuse resources and credentials across releases: roles, schemas, the bucket, its restricted account and the
database logins are created once.

## Rollback

- **Frontend:** redeploy the previous artifact with its matching public configuration; no database change.
- **Application images after a schema change:** selecting an older image does not work by itself, because
  the older image's schema revision no longer matches the database and it will report not ready. Options are
  to restore the pre-release database backup together with the older images (losing data written since), or
  to roll forward with a fix. Reverting a commit or choosing an older container is not a safe data rollback.
- **Configuration:** restore the previous values and restart.
- There is no implemented rollback automation or tested rollback procedure.

## Known gaps

These are unresolved and affect real deployments; each needs input from the platform owners.

- No infrastructure-as-code, deployment pipeline, registry, environment inventory, secrets management,
  ingress/TLS, DNS or scaling configuration exists in the repository.
- How database logins **assume** the `NOLOGIN` runtime roles in production is not defined. The ingestion
  README says the login must assume `ingestion_runtime`; the example `.env` uses a separate login name;
  Compose logs in as the role directly. If a login assumes a role with `SET ROLE` or `-crole=`, the
  role-level `search_path`, `statement_timeout` and `lock_timeout` from `runtime_grants.sql` do not apply
  ([Data model](../reference/data-model.md#roles-and-privileges)). It was also not verified whether a
  connection-option role in a DSN is preserved by the ingestion pool setup.
- No zero-downtime or rolling-update procedure given the strict revision check; no tested rollback.
- No production observability destination or alert rules ([Observability](../operations/observability.md#known-gaps)).
- Production backup and restore procedures are not defined (only the local ones, [Recovery](../operations/recovery.md)).
- The AWS IAM policy and S3 (non-MinIO) provisioning are not defined.
- GitHub branch protection and required checks are not configured yet ([`TODO.md`](../../TODO.md#github-ci-activation)).
