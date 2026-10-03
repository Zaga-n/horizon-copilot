# Shared libraries

`libs/` holds five Python packages that the chat and ingestion services (and, for the schema, the
migration job) share. They are packaged into the service images and are not deployed on their own.
Three libraries (genai, google-identity, observability) have their own README stating why they exist; this page summarises all five and the rules that keep them from becoming a dumping ground.

## Admission rule

A library exists only when **two deployables must agree on one rule** (the "extraction trigger"): the
embedding contract, who a bearer token represents, how telemetry is set up, the table definitions, the
policy file format. Libraries:

- take explicit, typed inputs and **read no environment** (one exception: the OpenTelemetry SDK still
  honours its own `OTEL_*` variables);
- never import a service, and do not depend on each other;
- do not hold prompts, schemas, retries, middleware, port mappings, span vocabulary or identity ports:
  those stay in each service.

The service-independence and `pydantic_settings` rules are enforced by import-linter contracts in the root [`pyproject.toml`](../../pyproject.toml), run by `scripts/quality.sh` and pre-commit (library-to-library independence holds today but is only partly contract-enforced):

- each library may not import the services, and only `horizon_config` may use `pydantic_settings`
  (`horizon_schema`, `horizon_genai`, `horizon_observability` and `horizon_google_identity` are forbidden
  from it);
- inside each service only designated layers may import a library: `horizon_schema` only from `db/`;
  `horizon_genai` from `genai/` and `bootstrap/`; `horizon_observability` from `observability/` and
  `bootstrap/`; `horizon_google_identity` from `adapters/` and `bootstrap/`; `horizon_config` from
  `config/` and `bootstrap/`;
- runtime services and `horizon_schema` may not import `horizon_migrations` or `alembic`.

## Libraries

| Package | Purpose | Main public API | Used by |
|---|---|---|---|
| `horizon_config` ([`libs/config`](../../libs/config/src/horizon_config)) | Strict layered YAML policy: `base.yaml`, environment, per-service layers; rejects YAML keys that are not allowed policy fields | `PolicyYamlSource`, `policy_layers`, `discover_policy_directory`, `ConfigurationError` | chat, ingestion settings |
| `horizon_genai` ([`libs/genai`](../../libs/genai/README.md)) | Bedrock connection policy, chat-model and embedding factories, provider error classification, finite-vector checks. SDK retries disabled: callers own retry policy | `BedrockConnection`, `bedrock_runtime_client`, `build_bedrock_chat_model`, `build_bedrock_embeddings`, `checked_vector`, `classify_bedrock_error`, `observe_titan_usage`, `GenAI*Error` types | chat (`genai/`, `bootstrap/`), ingestion (`genai/`, `bootstrap/`) |
| `horizon_google_identity` ([`libs/google-identity`](../../libs/google-identity/README.md)) | Google ID-token verification: signature, issuer, audience, expiry via google-auth, token-size bound, subject from `sub` only, bounded certificate fetch | `GoogleIdTokenVerifier`, `HttpCertificateRequest`, `InvalidIdTokenError`, `CertificatesUnavailableError`, `VerifiedClaims`, `MAX_TOKEN_CHARS` | chat and ingestion `adapters/identity.py` |
| `horizon_observability` ([`libs/observability`](../../libs/observability/README.md)) | OpenTelemetry provider lifecycle, allowlisting JSON log formatter, span error marking, trace-context carriers | `open_providers`, `ResourceIdentity`, `JsonLogFormatter`, `install_json_logging`, `boundary_span`, `mark_error`, `inject_carrier`, `extract_link`, `DEFAULT_SAMPLER` | both services' `observability/` |
| `horizon_schema` ([`libs/schema`](../../libs/schema/src/horizon_schema)) | Shared SQLAlchemy Core metadata (schema `app`), all table definitions, `EMBEDDING_DIMENSIONS = 1024`, `SCHEMA_REVISION` | `metadata`, `APP_SCHEMA`, `EMBEDDING_DIMENSIONS`, `SCHEMA_REVISION` | both services' `db/`, the migration job |

## Consequences for changes

- **Schema changes ripple.** `SCHEMA_REVISION` is compiled into every runtime image and compared to
  `app.alembic_version` at readiness. Changing `libs/schema` requires a migration revision, a new revision
  constant, and rebuilding all three images ([Migration job](../services/migrations.md#adding-a-revision)).
- **Identity rules are shared on purpose.** A change to token verification applies to both APIs at once.
- **The embedding contract is shared on purpose.** Chat embeds queries and ingestion embeds documents
  into the same vector index; changing model, dimension or normalisation in one place changes both and
  needs re-indexing of existing documents. The 1024 dimension is also fixed in the `vector(1024)` column
  and in settings validators.
- **Adding a library** requires meeting the admission rule; adding its package to `root_packages` in the import-linter configuration (otherwise its contracts do not apply) and to `known-first-party` in the Ruff isort settings; contracts for its boundary; a `COPY libs/<name> libs/<name>` line in each consuming service's Dockerfile; and a `COPY …/pyproject.toml` line in every Dockerfile that runs `uv sync --locked` against the workspace.

Known detail: the coverage configuration in `pyproject.toml` lists the other packages but omits
`horizon_config`, so its tests run but are not measured.
