# Authentication

Who can call Horizon, how identity is established, and how to obtain and reuse a token for testing. For
the security model as a whole see [System architecture](../architecture/system.md#trust-boundaries-and-security-model).

## Identities in the system

| Identity | Kind | How it authenticates | Where it is configured |
|---|---|---|---|
| End user | Human | Google sign-in produces an **ID token**; APIs verify it | `GOOGLE_CLIENT_ID` (public OAuth client) |
| Local developer | Human, development only | No token; a fixed subject | `identity_mode: local` ([below](#local-identity-mode)) |
| Chat / ingestion runtime | Machine, to PostgreSQL | Database logins acting as `chat_runtime` / `ingestion_runtime` | [Data model](../reference/data-model.md#roles-and-privileges) |
| Migration job | Machine, to PostgreSQL | Logins acting as `app_migrator` / `checkpoint_migrator` | [Migration job](../services/migrations.md) |
| Ingestion | Machine, to MinIO | Dedicated access key and secret | `INGESTION_MINIO_ACCESS_KEY` / `…SECRET_KEY` |
| Chat and ingestion | Machine, to AWS | Static AWS keys or the SDK credential chain | [Integrations](../reference/integrations.md#amazon-bedrock) |

There is **no service-to-service identity**: the APIs never call each other, and there are no API keys,
service accounts, roles or scopes for API callers. Programmatic callers must present a Google ID token like
any user; the repository defines no non-interactive way to obtain one.

## How the APIs verify a caller

Every `/v1` route of both APIs requires `Authorization: Bearer <Google ID token>`.

- The token must be a Google **ID token** (not an access token, and not a developer API key) whose
  **audience** equals the configured client ID (`GOOGLE_CLIENT_ID` for chat, `INGESTION_GOOGLE_CLIENT_ID`
  for ingestion; use the same value in both). Signature, issuer and expiry are checked with google-auth.
- The owner key is the verified **`sub`** claim only (1–255 characters). Email is ignored; there is no
  allowlist, domain restriction or role. Any Google account with a valid token for the client ID can sign
  in and gets its own isolated namespace.
- Missing, malformed, forged, expired, oversize (> 16384 characters) or wrong-audience tokens return 401
  (`invalid_identity`). If Google's signing certificates cannot be fetched the APIs return 503, so an outage
  is distinguishable from a bad token. Each verification is expected to fetch certificates
  ([Integrations](../reference/integrations.md#google-identity)).
- The same Google account must be used for uploading documents and chatting: documents are private to the
  uploader, and a different `sub` sees none of them.
- No OAuth client secret belongs in either API or the frontend.

### One-time Google setup

1. In Google Cloud, create an OAuth client of type **Web application** and note its client ID.
2. Add the frontend's exact origin to **Authorized JavaScript origins** (local Compose:
   `http://localhost:3000`). No redirect URI is needed for the ID-token button flow.
3. Put the client ID in the root `.env` as `GOOGLE_CLIENT_ID` (Compose passes it to both APIs and the
   frontend). For a host-run frontend, put it in `services/frontend/public/config.js`.

Changing the client ID means updating both APIs and the frontend configuration and restarting them; tokens
issued for the old audience stop working. Nothing server-side stores or rotates credentials for this flow.

## Getting a token for API testing

**Signed-in browser session (works with the repository as is).** Sign in through the frontend, open the
browser's developer tools, find any request to `http://localhost:8080/v1/…` or `http://localhost:8081/v1/…`
and copy the value after `Bearer ` from its `Authorization` header. This is a manual technique, not a
repository feature, and the frontend keeps the token only in memory, so it cannot be read back from storage.

Reuse it from a fresh terminal by exporting it for that shell:

```zsh
read -r "GOOGLE_ID_TOKEN?Paste the ID token: "
export GOOGLE_ID_TOKEN
curl -fsS -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" http://localhost:8080/v1/conversations
# 200 with a valid token; 401 if it is expired or has the wrong audience
```

Google ID tokens are short-lived (check the `exp` claim); there is no refresh in the repository, so sign in
again and copy a new one when you see 401. Do not commit or log the token.

**Local identity mode** avoids tokens entirely for backend testing, below.

## Local identity mode

For development without Google, each API can accept every request as one fixed owner,
`local-horizon-user` (`local_subject`, the same value in both services). It is deliberately hard to enable
by accident. A service refuses to start with `identity_mode: local` unless **both** hold:

- `ENVIRONMENT_NAME` (ingestion: `INGESTION_ENVIRONMENT_NAME`) is `local`;
- the bind host is `localhost` or a loopback address.

`config/local.yaml` sets `identity_mode: local` for the `local` environment, so a host-run service with
`ENVIRONMENT_NAME=local` defaults to it. The Compose containers force `IDENTITY_MODE=google` and bind
`0.0.0.0`, so containers never use it; a Docker port mapping does not make an internal public binding
loopback.

To use it, run the API process(es) on the host against the Compose infrastructure (stop the container
versions first to free ports 8080 and 8081). The required variables are listed in
[Configuration](../reference/configuration.md); a minimal chat example (substitute placeholders; the
database password is `CHAT_DATABASE_PASSWORD` from `compose.yaml`, and `GOOGLE_CLIENT_ID` must be present
even though local mode does not use it):

```zsh
docker compose stop chat ingestion ingestion-worker   # after `docker compose up -d` has completed; frees ports 8080/8081
export ENVIRONMENT_NAME=local BIND_HOST=127.0.0.1 BIND_PORT=8080
export FRONTEND_ORIGIN=http://localhost:3000 AWS_REGION=eu-west-1
export MAIN_MODEL_ID='<main-profile-id>' UTILITY_MODEL_ID='<utility-profile-id>'
export EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
export GOOGLE_CLIENT_ID=unused.apps.googleusercontent.com
export DATABASE_DSN="postgresql://chat_runtime:<CHAT_DATABASE_PASSWORD>@localhost:5432/horizon"
export CHECKPOINT_DATABASE_DSN="$DATABASE_DSN"
uv run --locked --package horizon-chat python -m horizon_chat.main
```

Both settings classes also read a `.env` from the working directory, which at the repository root is the Compose file: chat will pick up `GOOGLE_CLIENT_ID`, the model IDs and the AWS keys from it, while ingestion needs its `INGESTION_`-prefixed names. Ingestion has the same pattern with its `INGESTION_*` variables (database login `ingestion_runtime`,
MinIO at `http://localhost:9000`, the `INGESTION_MINIO_*` account) and two processes
(`… horizon_ingestion.main api` and `… worker`); the complete variable lists are in [`services/chat/.env.example`](../../services/chat/.env.example) and [`services/ingestion/.env.example`](../../services/ingestion/.env.example), including `INGESTION_MINIO_BUCKET` and `INGESTION_EMBEDDING_MODEL_ID`. These host-mode procedures were checked against the
settings and `dev/stack/README.md` but not executed during documentation. AWS credentials are still
needed for real answers and indexing.

The frontend can use local identity only under `npm run dev` on loopback, by setting
`localIdentity: true` in `services/frontend/public/config.js`
([Frontend](../services/frontend.md#runtime-configuration)). Data created under local identity belongs to
`local-horizon-user`, not to any Google account.

## Credential handling rules

- Tokens, AWS keys, database passwords and MinIO keys never belong in docs, logs or the repository. Logs
  and telemetry are allowlisted and never contain tokens.
- The shared defaults in `compose.yaml` are for local development only.
- Rotation: there is no stored user credential to rotate. Rotating AWS, MinIO or database credentials means
  updating the environment and restarting; for existing local volumes, changing a password variable does not
  change accounts already created ([Local deployment](local-deployment.md#reset)).
