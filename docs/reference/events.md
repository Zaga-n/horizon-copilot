# Events and queues

Horizon has two asynchronous interfaces: the Server-Sent Events (SSE) stream that carries a chat
answer to the browser, and the PostgreSQL-backed job queue that carries uploaded documents from the
ingestion API to its worker. There is no external message broker and no webhook or outbound event
feed.

## Chat turn stream (SSE)

Produced by `POST /v1/conversations/{id}/turns:stream` and
`POST /v1/conversations/{id}/turns/{turn_id}:retry` (see [API](api.md#chat-api)); implemented in
[`application/_turn_stream.py`](../../services/chat/src/horizon_chat/application/_turn_stream.py),
[`api/sse.py`](../../services/chat/src/horizon_chat/api/sse.py) and
[`domain/streaming.py`](../../services/chat/src/horizon_chat/domain/streaming.py).

### Transport

- Status 200, `Content-Type: text/event-stream; charset=utf-8`, `Cache-Control: no-cache, no-store`,
  `X-Accel-Buffering: no`. A replayed idempotency key does **not** stream: it answers with
  `application/json` (a `Turn`), so clients must branch on `Content-Type`.
- The endpoint is a POST, so browsers cannot use `EventSource`; read the response body with `fetch`.
- Each event is framed exactly as:

  ```text
  id: <run_id>:<sequence>
  event: <type>
  data: <json envelope>

  ```
- **No keep-alive comments, no `retry:` field and no `Last-Event-ID` resume.** An intermediary with a
  short idle timeout can cut a long silent phase (for example during retrieval); a reverse proxy
  must also not buffer or compress this response.

### Envelope

Every event's `data` is one JSON object (null fields are omitted):

| Field | Meaning |
|---|---|
| `event_id` | `"<run_id>:<sequence>"`, equal to the SSE `id` |
| `type` | Event type, equal to the SSE `event` name |
| `conversation_id`, `turn_id`, `run_id`, `assistant_message_id` | Identifiers of this attempt |
| `attempt_number` | 1 for the first attempt, +1 per retry |
| `trace_id` | 32-hex OpenTelemetry trace ID of this attempt ([Observability](../operations/observability.md)) |
| `sequence` | Starts at 1 and increases by 1 for each event **within one attempt**; a retry is a new run with a new `run_id` and its own sequence |
| `data` | Type-specific payload (below) |

### Event types

| `type` | `data` | Notes |
|---|---|---|
| `started` | `{}` | Always first (sequence 1); sent before the checkpoint is pinned and before the agent runs |
| `progress` | `{phase, message}` | `phase` is `guardrail`, `retrieval` or `generation`; `message` is a fixed English display string. Only emitted when the phase changes. Not persisted |
| `delta` | `{text}` | An incremental fragment; concatenate in order |
| `sources` | `{sources: [Source…]}` | Emitted once, **only on success**, after the database commit and immediately before `completed`; may be empty. Only cited sources, in first-citation order. `unavailable` is always false here |
| `completed` | `{}` | Terminal |
| `failed` | `{failure_category, persistence_pending, retry_available, text}` | Terminal. `text` is a constant sentence; it is not an error message |

`Source` fields are listed in the [API reference](api.md#models).

**Progress.** The first `progress` is always `guardrail`, right after `started`. `retrieval` is sent
when the search tool starts and `generation` when the final model call starts. An answer that the
guardrail declines (`ambiguous`, `out_of_scope`, `injection`) produces `guardrail` and then a single
`delta` with the fixed reply, with no retrieval or generation progress; the "no evidence found" reply
is likewise a single `delta`. Only the final, tool-free model call otherwise produces `delta` text:
internal reasoning, decisions, guardrail output, summaries and tool results are never streamed.

**Failure categories** (`failed.data.failure_category`): `interrupted`, `cancelled`,
`service_unavailable`, `provider_rejected`, `provider_protocol`, `index_integrity`,
`budget_exhausted`, `invalid_citations`, `internal`. The cause-to-category table is in the
[chat service page](../services/chat.md#failure-handling-and-recovery).

### Ordering and persistence guarantees

- The admission commit precedes the response, so nothing in the stream describes an unsaved turn.
- For each `delta`: the accumulated text is saved (one database transaction, which also checks the
  lease) **before** the event is yielded, so a visible prefix is always durable.
- On success the completed message and its sources are committed **before** `sources` and
  `completed` are sent.
- On failure the failure is written (with bounded retries) before `failed` is sent, unless the write
  cannot be confirmed (`persistence_pending=true`).
- Exactly one terminal event (`completed` or `failed`) is sent while the connection is open, and
  nothing follows it. If the connection drops or the process dies, **no terminal event arrives**.

### `failed` flags

| `persistence_pending` | `retry_available` | Meaning |
|---|---|---|
| false | true | Failure stored; the user may retry the newest attempt with a fresh key |
| true | false | The failure could not be confirmed in the database. Do **not** retry; the service keeps a repair intent and persists it when the database returns. Read the turn for the authoritative status |
| false | false | The turn is no longer retryable here: it was already completed (a lost commit acknowledgement), or the conversation was purged |

### Disconnects and reconciliation

If the client disconnects, the stream is cancelled and the attempt is recorded as `cancelled`,
keeping the partial text and releasing the lease; it is retryable. If the process dies or the
repair buffer overflows, the attempt becomes `interrupted` when its lease (150 s) next gets
inspected. To recover state, call `GET /v1/conversations/{id}/turns/{turn_id}` (statuses, attempts,
failure category, `retry_available`) and `GET /v1/conversations/{id}/messages` (saved content,
sources, feedback). A partially streamed answer appears there as a `failed` assistant message with the
saved prefix.

### Client behaviour in this repository

The [frontend](../services/frontend.md#chat-behaviour) decodes the stream strictly: it requires a
contiguous sequence starting at `started`, one `run_id`, matching `event:`/`type` names and valid
payloads, and treats a stream that ends without a terminal event as an unknown outcome to reconcile.
A `: heartbeat` comment line is tolerated by its decoder, but the backend never sends one.

## Ingestion job queue

All durable work for the worker is a row in `app.ingestion_jobs`; PostgreSQL `NOTIFY` only shortens
the wait. Implemented in
[`db/queue.py`](../../services/ingestion/src/horizon_ingestion/db/queue.py),
[`db/fencing.py`](../../services/ingestion/src/horizon_ingestion/db/fencing.py) and
[`workers/jobs.py`](../../services/ingestion/src/horizon_ingestion/workers/jobs.py).

| Aspect | Behaviour |
|---|---|
| Queue | Table `app.ingestion_jobs` (kinds `index`, `delete`, `superseded_cleanup`; statuses `queued`, `processing`, `retrying`, `ready`, `failed`, `cancelled`) |
| Wake-up channel | PostgreSQL `LISTEN`/`NOTIFY` on channel **`ingestion_jobs`**, payload-free. Sent on upload commit, publication, user retry, delete and delete re-queue |
| Subscription order | The worker subscribes before its first scan and re-subscribes after a connection loss (backoff from 2 s doubling to 10 s). Notifications can be lost, so correctness never depends on them |
| Due-work scan | Every `scan_interval_seconds` (2 s) and after any finished claim. Finds `queued`, `processing` or `retrying` jobs with `next_retry_at <= now()` and no unexpired lease, ordered by `next_retry_at, id`. Retry timers and expired leases are discovered only by this scan |
| Claim | `FOR UPDATE SKIP LOCKED`; sets `processing`, `generation + 1`, `lease_owner`, `lease_until = now + job_lease_seconds` (90 s) |
| Lease and heartbeat | The owner extends the lease every `heartbeat_interval_seconds` (20 s). If a worker dies, the lease expires and another worker claims the job with a higher generation |
| Fencing | Every result write requires the job's `generation`, `lease_owner`, an unexpired lease, `processing` status and the right document lifecycle. A stale worker's writes are rejected and it stops |
| Ordering | Best effort by `next_retry_at`; no per-document or global ordering guarantee beyond one active index job per document |
| Delivery | A job may be attempted more than once (retries, lease expiry, worker restart); results are idempotent through the fence, deterministic chunk IDs and "unfinished chunks only" resumption. No exactly-once claim is made |
| Deduplication | Owner-scoped idempotency keys (`upload_requests`), live-content uniqueness, and unique partial indexes: one index job per version, one active index job per document, one delete job per document |
| Retry | Automatic with backoff for transient categories, up to `max_job_attempts` (3) per retry cycle; then `failed`. User retry re-queues a failed index job in place; repeating `DELETE` re-queues a failed cleanup job |
| Dead-letter | None. `failed` is the terminal state; there is no separate dead-letter store. Find failures with the status API or SQL on `app.ingestion_jobs` |
| Replay constraints | Re-queuing keeps completed chunk vectors. Do not hand-edit job rows; use the retry and delete routes, which bump the generation and clear leases consistently |

Worker acknowledgement boundary: the API acknowledges an upload (202) only **after** the original is
stored and the document, version, job and idempotency mapping have been committed. The worker records
progress per chunk and marks a job `ready` only inside the fenced publication transaction.

Trace propagation: the upload's W3C trace context is stored in `ingestion_jobs.trace_context`, and
the worker's `app.job` span is linked to it ([Observability](../operations/observability.md#ingestion-upload-and-job-are-linked-not-parented)).

## Implementation references

[`services/chat/src/horizon_chat/domain/streaming.py`](../../services/chat/src/horizon_chat/domain/streaming.py),
[`services/chat/src/horizon_chat/api/sse.py`](../../services/chat/src/horizon_chat/api/sse.py),
[`services/ingestion/src/horizon_ingestion/db/queue.py`](../../services/ingestion/src/horizon_ingestion/db/queue.py),
[`services/ingestion/src/horizon_ingestion/domain/lifecycle.py`](../../services/ingestion/src/horizon_ingestion/domain/lifecycle.py).
