# horizon-genai

Bedrock connection policy, embedding and chat-model construction, and provider error
classification shared by `horizon-chat` and `horizon-ingestion`.

**Kind:** genai. **Importers:** each service's `genai/` package, and `bootstrap/` to build the
`BedrockConnection` input from settings and secrets. Enforced by import-linter contracts in the
workspace `pyproject.toml`.

## Why this is shared

Chat embeds queries and ingestion embeds documents into the same pgvector index. The model id,
dimensions and `normalize=True` are a cross-service compatibility contract: if the two copies
drift, retrieval silently degrades. The copies had already drifted (ingestion built its client on
the event loop, without static credentials, and patched the SDK client to read token usage).

**Extraction trigger:** two deployables must agree on one rule (the embedding contract and the
Bedrock error classification). Prompts, schemas, retries, middleware and port mappings stay in
each service.

## Contract

- One call is one physical attempt: SDK retries are disabled; callers own retry policy.
- No environment reads: callers pass resolved configuration.
- Each service translates `GenAIProviderUnavailableError`, `GenAIProviderRejectedError` and
  `GenAIProtocolError` into its own port errors exactly once.
