# Overview

## What Horizon Copilot does

Horizon Copilot is a chat assistant for **Horizon Europe** work: programmes, projects, proposals, work
packages, deliverables, funding and project management. People sign in with Google, upload their own
project documents (PDF or Word), and ask questions. The assistant answers general Horizon Europe
questions from its model knowledge and is instructed to answer questions about named projects or specific documents
**only from evidence retrieved from the user's uploaded documents**, with citations to the file, page or
section it used. If the indexed evidence does not support a project claim, it is instructed to say so
rather than guess ([agent behaviour](services/chat.md#the-horizon-agent)).

Who uses it: individual users through the web frontend. Their documents and conversations are private to
their Google account. Operators run it as a set of containers.

## Concepts and terms

| Term | Meaning |
|---|---|
| **ID token** | The Google-issued JWT the browser obtains at sign-in and sends as `Authorization: Bearer …`. It must be issued for the configured OAuth client (the *audience*) |
| **Owner / subject** | The verified `sub` claim of the ID token. All data is scoped to it. There are no teams, admins or sharing |
| **Conversation** | A chat thread with an ordered list of messages. Inactive conversations are deleted after 30 days |
| **Turn** | One user question and everything done to answer it; identified by an idempotency key |
| **Attempt (run)** | One try at answering a turn. A failed attempt can be retried, creating a new attempt with its own trace |
| **SSE** | Server-Sent Events: the one-way stream that carries an answer to the browser as it is written ([events](reference/events.md)) |
| **Idempotency key** | A client-chosen key making a request safe to repeat: the same key and content returns the earlier result instead of doing the work again |
| **Lease** | A time-limited claim stored in the database: a conversation is leased while an attempt runs, and a job is leased while a worker processes it. Expired leases are recovered |
| **Agent** | The bounded LangGraph/LangChain program that decides whether to search, retrieves evidence and writes the answer |
| **Guardrail** | A first model call that answers out-of-scope or manipulative input with a fixed refusal, and asks a clarifying question when the request is ambiguous |
| **Document, version, chunk** | A document is what the user uploaded; each upload attempt is a *version*; a published version is split into overlapping text *chunks* with vector embeddings |
| **Embedding / pgvector** | A 1024-number vector per chunk (Amazon Titan v2) stored in PostgreSQL; questions are matched to chunks by cosine similarity |
| **Job** | A durable unit of ingestion work (index, delete, or clean up a superseded version) that the worker claims |
| **Published** | Only a version that is fully embedded and published is searchable; replacing a document keeps the old version searchable until the new one publishes |
| **Citation / source** | A marker like `[S1]` in an answer tied to a chunk's file, page or section |
| **Tombstone** | What remains of a deleted document: a content-free row |
| **Inference profile** | The Bedrock model identifier configured for the main and utility chat models |

## Components at a glance

| Component | One-line role | Page |
|---|---|---|
| Frontend | Browser workspace: sign-in, chat, feedback, documents | [Frontend](services/frontend.md) |
| Chat service | Conversations, streaming answers, agent, retention | [Chat](services/chat.md) |
| Ingestion service | Upload API plus worker that indexes documents | [Ingestion](services/ingestion.md) |
| Migration job | Prepares the database schema | [Migration job](services/migrations.md) |
| PostgreSQL + pgvector | All durable state and vector search | [Data model](reference/data-model.md) |
| MinIO | Versioned storage of original files | [Ingestion](services/ingestion.md) |
| AWS Bedrock | Chat models and embeddings | [Integrations](reference/integrations.md) |
| Local telemetry stack | Collector, Grafana, Langfuse, Alloy | [Observability](operations/observability.md) |

The system diagram and communication table are on [System architecture](architecture/system.md); how a
question and an upload travel through the parts is on [Request lifecycle](architecture/request-lifecycle.md).

## Design properties worth knowing

- **Durable before visible.** A streamed answer is saved before the browser sees each fragment, and a
  completed answer is committed before the completion event, so a dropped connection never loses text the
  user saw.
- **Safe to repeat.** Uploads, turns and retries use idempotency keys; workers resume interrupted jobs.
- **Bounded.** Every agent attempt has call, time and evidence budgets; every ingestion job has retry,
  time and concurrency budgets.
- **Separated privileges.** Chat can read but not write documents; ingestion cannot read conversations;
  neither can change the schema.
- **Content-free telemetry.** Logs, spans and metrics carry identifiers and categories, not prompts,
  answers or document text.

## Scope and limits

Out of scope today: OCR (image-only PDFs fail), legacy `.doc` files, sharing documents between users,
file history or rollback, per-user quotas and rate limits, alerting, and any production infrastructure
definition ([Deployment](guides/deployment.md#known-gaps)). Retrieval is a single vector search; hybrid
retrieval and use of traces and feedback for improvement are tracked as open items in
[`TODO.md`](../TODO.md). Live AWS and signed-in verification are manual
([Smoke test](guides/smoke-test.md)).

## Where to go next

| I want to… | Read |
|---|---|
| Run it | [Local deployment](guides/local-deployment.md) |
| Understand a request end to end | [Request lifecycle](architecture/request-lifecycle.md) |
| Change code | [Development](guides/development.md) |
| Call the APIs | [API reference](reference/api.md), [Authentication](guides/authentication.md) |
| Diagnose a problem | [Troubleshooting](operations/troubleshooting.md) |
