# Integrations

Every outbound dependency of the deployables, the operation actually performed, how it authenticates,
and how failures are handled. Only operations the code performs are listed; provider contracts were not
independently verified.

| Dependency | Caller | Operation | Required locally? |
|---|---|---|---|
| Amazon Bedrock (Converse) | chat | `Converse` / `ConverseStream` through LangChain's `ChatBedrockConverse` | Yes, for real chat answers |
| Amazon Bedrock (Titan embeddings v2) | chat, ingestion worker | `InvokeModel` on `amazon.titan-embed-text-v2:0` through `BedrockEmbeddings` | Yes, for real questions and for indexing |
| Google (token certificates) | chat, ingestion APIs | HTTPS GET of Google's public signing certificates during every token verification | Yes in Google identity mode; not in local mode |
| Google Identity Services | browser (frontend) | Loads `https://accounts.google.com/gsi/client`; receives the ID token | Yes for Google sign-in |
| PostgreSQL + pgvector | chat, ingestion, migrations | SQL; `LISTEN`/`NOTIFY` (ingestion) | Yes |
| MinIO (S3 API) | ingestion API and worker | `PutObject`, `HeadObject`, `GetObject`, `ListObjectVersions`, `DeleteObject` (all with exact version IDs where applicable) | Yes, for uploads |
| OpenTelemetry Collector | chat, ingestion | OTLP/HTTP `/v1/traces`, `/v1/metrics` | Optional |
| Langfuse, Grafana LGTM, Loki (via Alloy) | Collector, Alloy | See [Observability](../operations/observability.md) | Optional, local only |

## Amazon Bedrock

Built in [`libs/genai/src/horizon_genai/bedrock.py`](../../libs/genai/src/horizon_genai/bedrock.py),
shared by both services so the embedding contract (model, 1024 dimensions, `normalize=True`) cannot drift
between query-time and index-time embedding.

- **Authentication.** Static `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (plus `AWS_SESSION_TOKEN` for
  temporary credentials; ingestion uses the `INGESTION_AWS_*` names) if set, otherwise boto3's default
  credential chain. The principal needs permission to invoke the configured chat inference profiles and
  the Titan embedding model in the configured region. The exact IAM policy is not defined in the
  repository.
- **Calls.**
  - Chat models (`MAIN_MODEL_ID`, `UTILITY_MODEL_ID`): Converse API, `max_tokens` = `max_output_tokens`
    (4096), a `reasoning_effort` per role, and structured output via client tools for the guardrail. The
    final answer streams; decision and utility calls do not.
  - Embeddings: one `InvokeModel` call per text, 1024 dimensions, normalised. Returned vectors are
    rejected unless they contain exactly 1024 finite numbers.
- **Timeouts and retries.** Connect 5 s, read 30 s (`provider_*_timeout_seconds`). SDK retries are disabled
  (`total_max_attempts = 1`), so one call is one physical attempt and the services own retry policy
  ([chat](../services/chat.md#the-horizon-agent), [ingestion](../services/ingestion.md#retry-budgets)).
- **Error mapping** ([`libs/genai/src/horizon_genai/errors.py`](../../libs/genai/src/horizon_genai/errors.py)):
  a `ValidationException` that does not name a model identifier is *rejected* (not retried); any other
  client or SDK error is *unavailable* (retried when transient); a malformed response is a *protocol*
  error. A `ValidationException` that names a model identifier is treated as unavailable (a likely model
  access or configuration problem).
- **Effects.** Calls are billed to the account. Readiness never calls Bedrock, so a service can be ready
  with unusable credentials or model access.
- **Verification status.** Offline tests pin the request fields and parse real Converse stream event
  shapes using botocore stubs. They do not prove model access, quotas, regional availability or prompt
  behaviour. [`dev/stack/verification.md`](../../dev/stack/verification.md) records a dated live run, which this
  manual did not repeat. `services/ingestion/README.md` and `dev/stack/README.md` say Bedrock access is not verified, while that record shows a live run, so the repository's evidence is mixed.
- **Concurrency.** The Bedrock clients are synchronous boto3 clients driven from executor threads, so
  the default thread pool bounds concurrency in chat; ingestion bounds it with `vendor_concurrency`.

## Google identity

- **Server side** ([`libs/google-identity`](../../libs/google-identity/src/horizon_google_identity/verifier.py)).
  `google.oauth2.id_token.verify_oauth2_token` checks signature, issuer, expiry and that the audience equals
  `GOOGLE_CLIENT_ID` / `INGESTION_GOOGLE_CLIENT_ID`; the owner key is the `sub` claim only. Tokens over
  16384 characters are rejected before any work. The call runs in a worker thread.
- **Certificate fetch.** During verification google-auth requests Google's public certificate set. The repository's request adapter wraps a plain `httpx.Client` with **no caching** (chat: fixed 5 s timeout, environment proxies ignored; ingestion: the provider connect/read timeouts, 5 s / 30 s, and proxy environment variables honoured), and google-auth only caches when handed a cache-aware transport. Based on that, **each
  authenticated API request is expected to cause one outbound HTTPS call to Google** (not measured at
  runtime). A fetch failure returns 503 `identity_unavailable` (chat) or 503 `service_unavailable`
  (ingestion), distinct from 401 for a bad token. Egress to Google must therefore be open from both API
  containers in any Google-identity deployment.
- **Authorization policy.** There is no email, domain or allowlist check. Any Google account holding a
  valid ID token for the client ID obtains its own isolated data namespace.
- **Browser side.** The frontend loads Google Identity Services and receives an ID token; no OAuth client
  secret exists anywhere in the system ([Authentication](../guides/authentication.md)).

## MinIO (S3-compatible object storage)

Client: [`services/ingestion/.../adapters/storage.py`](../../services/ingestion/src/horizon_ingestion/adapters/storage.py).

| Operation | Used for |
|---|---|
| `PutObject` at `<object_prefix><32-hex>.<pdf\|docx>` (default prefix `attempts/`) | Storing an upload; the response must carry a version ID, otherwise the upload fails with 503 `service_unavailable` (internal reason `versioned_bucket_required`) |
| `HeadObject` with `VersionId` | Confirming the object still exists at commit time |
| `GetObject` with `VersionId` | The worker downloading the original for extraction |
| `ListObjectVersions` under the prefix | Orphan reconciliation |
| `DeleteObject` with `VersionId` | Exact-version cleanup on deletion, supersession and orphan removal |

Authentication is a dedicated access key and secret (`INGESTION_MINIO_ACCESS_KEY` / `…SECRET_KEY`),
independent of AWS credentials. The provisioned policy
([`dev/stack/minio/ingestion-policy.json`](../../dev/stack/minio/ingestion-policy.json)) allows bucket
location/list/list-versions/versioning reads on `horizon-documents` and object put/get/delete (including
versions) only under `horizon-documents/attempts/*`. Chat has no access to the bucket: it never reads
original files. The S3 client uses the provider connect/read timeouts (5 s / 30 s) with SDK retries disabled. Every S3 failure, including an access denial, surfaces as `storage_unavailable` (index jobs retry it until the job budget is spent; cleanup jobs retry indefinitely); the adapter never produces `integrity_failed`. The code's storage client uses the S3 API generally, but only MinIO
provisioning is defined in the repository; using AWS S3 would need an equivalent bucket and policy
(not defined here).

## PostgreSQL

Details of roles, grants and ownership: [Data model](data-model.md#roles-and-privileges). Connection
behaviour: chat uses a SQLAlchemy async engine plus a separate psycopg pool for LangGraph checkpoints;
ingestion uses one pool plus a dedicated autocommit connection for `LISTEN`. Pool sizes, connect and
statement timeouts are policy settings ([Configuration](configuration.md)). The queue channel is described
in [Events](events.md#ingestion-job-queue).

## Telemetry backends

The services speak only OTLP/HTTP to a Collector. Collector, Langfuse, Grafana and Loki wiring is local-only
and documented in [Observability](../operations/observability.md). Export failures never affect request
handling: exporters have a 2 s timeout and the Collector is not a readiness dependency.

## What can be replaced or faked

| Dependency | In tests | In local development |
|---|---|---|
| Bedrock chat and embeddings | Scripted/fake models and embeddings; botocore stubs | Real AWS credentials needed for real answers and indexing |
| Google identity | Fake verifiers and fake tokens | `identity_mode: local` when the service runs on the host with a loopback bind ([Authentication](../guides/authentication.md#local-identity-mode)) |
| MinIO | Real disposable MinIO in integration tests | Provided by Compose |
| PostgreSQL | Real disposable database in integration tests | Provided by Compose |
| Collector and backends | In-memory span exporters | Optional; unset `OTLP_ENDPOINT` to disable export |

## Implementation references

[`libs/genai`](../../libs/genai/src/horizon_genai),
[`libs/google-identity`](../../libs/google-identity/src/horizon_google_identity),
[`services/ingestion/src/horizon_ingestion/adapters/storage.py`](../../services/ingestion/src/horizon_ingestion/adapters/storage.py),
[`services/chat/src/horizon_chat/bootstrap/runtime.py`](../../services/chat/src/horizon_chat/bootstrap/runtime.py).
