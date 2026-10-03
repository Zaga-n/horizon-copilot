# Request lifecycle

Traced end-to-end paths through the system. Each step names the component and, where it helps, the code
entrypoint; contracts live in the [API](../reference/api.md) and [Events](../reference/events.md)
references.

## Sign-in and an authenticated request

1. The browser loads the frontend and `/config.js`, then loads Google Identity Services and shows the
   sign-in button ([Frontend](../services/frontend.md#authentication-and-session-handling)).
2. Google returns an **ID token** to the page. The frontend decodes `sub` and `exp` (without verifying) and
   keeps the token in memory.
3. Every API call carries `Authorization: Bearer <token>`.
4. The API verifies the token: signature, issuer, expiry and audience, by fetching Google's signing
   certificates over HTTPS ([Integrations](../reference/integrations.md#google-identity)). The verified
   `sub` becomes the owner key; the `users` row is created on first use.
5. When the token expires the frontend signs the user out; there is no refresh.

## Asking a question (chat turn)

1. **Conversation.** `POST /v1/conversations` creates a conversation owned by the caller.
2. **Submit.** `POST /v1/conversations/{id}/turns:stream` with an `Idempotency-Key` and the text.
3. **Admission.** Buffered failure intents for the conversation are persisted first (separate transactions). Then, in one database transaction with the conversation row locked: convert an expired lease into an `interrupted` failure; resolve the idempotency key (replay
   returns the stored turn; different content is 409), reject if another attempt is active (409), then
   create the turn, user message, pending assistant message and run, and lease the conversation for
   150 s. The HTTP response begins only after this commits.
4. **Stream starts.** `started` is sent; the LangGraph checkpoint this attempt resumes from is pinned.
5. **Guardrail.** The utility model classifies the question. Out-of-scope, ambiguous or injection-like
   input ends the run with a fixed reply (one `delta`) and no retrieval.
6. **Decide and retrieve.** The decision model may call `rag_search` (at most twice). The query is
   optionally rewritten, embedded with Titan and matched against the caller's published, completed
   chunks by cosine distance (≤ 8 chunks). If evidence was required but none exists, a fixed "no
   evidence" reply is sent without calling the final model.
7. **Answer.** The final, tool-free model streams text. For each fragment the service **saves the visible
   prefix, then sends the `delta`**.
8. **Validate citations.** After the stream, every `[S…]` marker must refer to this attempt's evidence and
   a search-requiring answer must cite something; otherwise the attempt fails (`invalid_citations`) and the
   saved text stays on the failed message.
9. **Complete.** The message, sources and run status commit in one transaction and the lease is released;
   then `sources` and `completed` are sent.
10. **Feedback.** The user may rate the answer (`PUT /v1/messages/{id}/feedback`) or comment on the
    conversation.

A retry (`:retry`) repeats steps 3–9 as a new attempt with a new run ID and trace ID, resuming from the
failed attempt's checkpoint.

Where each step is implemented: [Chat service](../services/chat.md#one-turn-step-by-step).

## Document upload to searchable

1. The frontend `POST /v1/documents` (multipart file, `Idempotency-Key`). Authentication precedes body
   parsing.
2. The ingestion API validates the file (extension, exact content type, size ≤ 50 MiB, non-empty,
   structural parse in an isolated subprocess).
3. It checks for a replay of the same key or identical live content (even if still processing or failed) and, if there is none,
   **stores the original** in MinIO under `attempts/` and **commits** a candidate version, an `index` job
   and the idempotency record, then sends `NOTIFY ingestion_jobs`. It answers **202** with a `status_url`.
4. The frontend polls `GET /v1/documents` (all pages of 100) every 2 s while any document is queued, processing, retrying or deleting, backing off to 30 s on errors.
5. The worker is woken by the notification, or finds the job at its next 2 s scan, and **claims** it
   (lease 90 s, generation fence).
6. **Extract** text in a time- and memory-bounded subprocess; **chunk** into 2000-code-point windows
   with 200 overlap; persist the full manifest (job moves to stage `embedding`).
7. **Embed** each chunk sequentially with Titan (1024 dimensions), holding a global concurrency permit
   only for each call; failed chunks are retried within budgets; progress is visible as
   `completed_chunks` / `total_chunks`.
8. **Publish** in one fenced transaction once every chunk is complete: the candidate becomes the
   document's published version (a replaced version is superseded and queued for cleanup) and the job is
   `ready`. Only now can chat retrieve the content.
9. A failed job (`failed`) can be retried from the UI or API; completed vectors are kept.

Details and edge cases: [Ingestion service](../services/ingestion.md).

## Deleting a document

1. `DELETE /v1/documents/{id}` sets the document `deleting` and unpublished in one transaction. Retrieval
   stops immediately, active worker writes for it are fenced off, and a `delete` job is queued.
2. The worker deletes each exact object version from MinIO, deletes the chunks and tombstones the
   rows; the document becomes `deleted`. The frontend polls until it disappears.
3. Saved chat answers keep their citation metadata, but history marks those sources `unavailable`.
4. A storage refusal keeps the cleanup job retrying indefinitely (the document stays `deleting` and out of retrieval, and cleanup completes once the cause is fixed); a job that reached `failed` is restarted by repeating the `DELETE`.

## When things go wrong

| Situation | What happens | Where recovered |
|---|---|---|
| Client disconnects mid-answer | Stream cancelled; attempt recorded `cancelled` with the saved prefix; retryable | Frontend reconciles from saved state |
| Chat process dies mid-turn | The conversation lease (150 s) lapses; the attempt becomes `interrupted` the next time a request admits, retries or reads that conversation (or retention fences it), then retryable; until then submissions get 409 | Lease expiry |
| Database unavailable when a turn fails | The `failed` event carries `persistence_pending=true` and no retry; an in-process intent is replayed by the reconciliation loop (5 s cadence, backing off during outages) and whenever that conversation's turn is submitted, retried or read | Reconciliation loop; lost if the process dies (then lease expiry) |
| Bedrock throttles or times out | Retried with backoff within budgets, never after visible text has started | Budgets, then `service_unavailable`/`budget_exhausted` |
| Worker dies mid-job | Lease lapses (90 s); another claim resumes unfinished chunks; a stale worker's writes are fenced | Periodic scan |
| `NOTIFY` is lost | Next scan (≤ 2 s) finds the job | Periodic scan |
| Crash between storing a file and committing the upload | An orphan object remains under `attempts/`; the client retries with the same key | Orphan reconciliation (older than 10 min and unreferenced) |
| Conversation inactive for 30 days | Marked `purging`, checkpoint and rows deleted; documents untouched | Retention loop |

Operational procedures for these are in [Troubleshooting](../operations/troubleshooting.md) and
[Recovery](../operations/recovery.md).
