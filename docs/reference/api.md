# API reference

Horizon exposes two HTTP APIs, both FastAPI services behind the same Google identity:

| API | Local address | Implementation | Purpose |
|---|---|---|---|
| Chat | `http://localhost:8080` | [`services/chat`](../../services/chat/src/horizon_chat/api) | Conversations, streaming turns, retry, feedback |
| Ingestion | `http://localhost:8081` | [`services/ingestion`](../../services/ingestion/src/horizon_ingestion/api) | Document upload, status, retry, deletion |

Both apps leave FastAPI's generated `/docs`, `/redoc` and `/openapi.json` enabled and
unauthenticated. The generated OpenAPI documents list only success and validation responses; the
domain errors below are not declared in them. SSE event contract: [Events](events.md).

## Conventions shared by both APIs

- **Base path.** Domain routes are under `/v1`. `/health` and `/ready` are unprefixed.
- **Authentication.** Every `/v1` route requires `Authorization: Bearer <Google ID token>` whose
  audience is the configured Google client ID. Chat accepts the `Bearer` scheme case-insensitively; ingestion requires the exact `Bearer ` prefix. The
  verified `sub` claim is the owner key. In local identity mode the token is ignored. Details,
  including how to obtain a token: [Authentication](../guides/authentication.md).
- **Ownership.** A caller can only see and modify their own data. Another owner's resources return
  **404** (never 403), indistinguishable from missing ones.
- **Idempotency.** Mutating requests that start work take an `Idempotency-Key` header, 1–200
  characters, free-form. Keys are scoped per owner (chat: per owner and conversation; turn and
  retry keys are separate namespaces).
- **CORS.** Only the configured frontend origin is allowed (`FRONTEND_ORIGIN` /
  `INGESTION_FRONTEND_ORIGIN`), without credentials. Allowed request headers: `Authorization`,
  `Content-Type`, `Idempotency-Key`. Chat allows `GET, POST, PUT, DELETE` and exposes only
  `Content-Type`; ingestion allows `GET, POST, DELETE` and exposes no response headers (so a
  browser cannot read `Location`, `Retry-After` or `WWW-Authenticate`). A preflight from another
  origin gets 400.
- **Error bodies differ by layer.** Do not assume one shape:

  | Source | Shape |
  |---|---|
  | Chat domain errors | `application/problem+json`: `{"type":"urn:horizon:<code>","title":"<code>","status":<n>}` |
  | Ingestion domain errors | `application/json`: `{"detail":"<code>"}` |
  | FastAPI validation (both) | 422 `{"detail":[{"type","loc","msg","input",…}]}`; **echoes the submitted input** |
  | Unknown route / method (both) | 404 `{"detail":"Not Found"}` / 405 `{"detail":"Method Not Allowed"}` |

  Domain error responses send `Cache-Control: no-store`; 401 adds `WWW-Authenticate: Bearer`.
  Neither API returns a request-ID header. Raw exception text never reaches a client.
- **Precedence.** On chat, authentication (401) runs before body validation, but malformed JSON is rejected with 422 first. On ingestion, FastAPI validates the `Idempotency-Key` header and path/query parameters (UUIDs, `limit`, `offset`) before authentication, so those return 422 even without a token; body parsing happens after authentication.
- **No rate limiting, request throttling or per-user quotas** exist in either application. Any such
  control would have to be applied at the deployment edge, which this repository does not define.

## Chat API

### Routes

| Method and path | Request | Success | Domain errors |
|---|---|---|---|
| `POST /v1/conversations` | none | 201 `Conversation` | 401, 503 |
| `GET /v1/conversations` | `cursor` (UUID, optional), `limit` 1–100 (default 50) | 200 `{conversations[], next_cursor}` | 404 (unknown cursor), 401, 503 |
| `GET /v1/conversations/{id}` | | 200 `Conversation` (includes conversation feedback) | 404, 401, 503 |
| `GET /v1/conversations/{id}/messages` | `cursor` (integer ≥ 0, default 0), `limit` 1–100 (default 50) | 200 `{messages[], next_cursor}` | 404, 401, 503 |
| `GET /v1/conversations/{id}/turns/{turn_id}` | | 200 `Turn` | 404, 401, 503 |
| `POST /v1/conversations/{id}/turns:stream` | header `Idempotency-Key`; body `{"content": "…"}` | 200 SSE stream (new attempt) **or** 200 JSON `Turn` (replay) | 404, 409 `idempotency_conflict` / `conversation_busy`, 401, 503, 500 |
| `POST /v1/conversations/{id}/turns/{turn_id}:retry` | header `Idempotency-Key`; body `{"expected_run_id": "<uuid>"}` | 200 SSE stream **or** 200 JSON `Turn` (replay) | 404, 409 `idempotency_conflict` / `conversation_busy` / `stale_retry`, 401, 503 |
| `PUT /v1/messages/{message_id}/feedback` | `{"rating": "like"\|"dislike", "comment": …}` | 204 | 404, 422 `feedback_target`, 401, 503 |
| `DELETE /v1/messages/{message_id}/feedback` | | 204 (idempotent) | 404, 401, 503 |
| `PUT /v1/conversations/{id}/feedback` | `{"rating": …\|null, "comment": …\|null}` | 204 | 404, 401, 503 |
| `DELETE /v1/conversations/{id}/feedback` | | 204 (idempotent) | 404, 401, 503 |

Bodies reject unknown fields (422). The two streaming routes return the 200 status for both outcomes:
**branch on the response `Content-Type`** (`text/event-stream` = a new attempt is streaming;
`application/json` = the key was already accepted and the stored turn is returned).

### Models

- `Conversation`: `id`, `status` (`active`; `purging` conversations are never returned),
  `created_at`, `updated_at`, `last_activity_at`, `title`, `feedback`. `title` is the first user
  message with whitespace collapsed, truncated to 72 characters, default "New conversation".
  `feedback` is `null` except on the detail route.
- `ConversationPage`: ordered by `created_at` descending; `next_cursor` is the last returned
  conversation ID when more exist.
- `Message`: `id`, `conversation_id`, `turn_id`, `attempt_number` (0 for user messages), `role`
  (`user`/`assistant`), `content`, `status` (`pending`/`completed`/`failed`), `sources[]`,
  `message_order`, `created_at`, `updated_at`, `run_id`, `trace_id`, `feedback`. History is ordered by
  `message_order` ascending; `next_cursor` is the last message's order when more exist. A failed
  assistant message keeps the visible partial answer.
- `Source`: `marker` (`S1`…), `chunk_id`, `document_id`, `document_version_id`, `title`, `filename`,
  `file_type`, optional `page`, `section_heading`, `start_offset`, `end_offset`, `locators[]`, and
  `unavailable`. History recomputes `unavailable` on each read: it is true when the document is not live (deleting or deleted), the version is retired or missing, or it has no stored object.
- `Turn`: `id`, `conversation_id`, `user_message_id`, `status` (`active`/`completed`/`failed`),
  `attempts[]`, `retry_available`. `retry_available` is true only when the turn's last attempt is
  failed **and** it is the conversation's newest attempt.
- `Run` (an attempt): `id`, `conversation_id`, `turn_id`, `user_message_id`, `assistant_message_id`,
  `attempt_number`, `trace_id`, `root_span_id`, `status`, `failure_category`, `retry_of_run_id`,
  `started_at`, `ended_at`, `agent_version`, `prompt_version`, `retrieval_version`.
- `content` limit: 1–16000 **characters** (not bytes); NUL is rejected; the text is not trimmed.
- Feedback `comment`: at most 4000 characters, trimmed, empty becomes null; a body needs a rating or a
  non-empty comment.

JSON responses include nulls; SSE `data` payloads omit null fields.

### Turn admission and idempotency

Admission completes **before** the HTTP response starts, so these outcomes are ordinary HTTP statuses and never stream events. Buffered failure intents are persisted first, each in its own transaction (a database outage there is 503); the remaining steps share one transaction under a lock on the conversation row.

**New turn** (`turns:stream`):

1. Buffered failure intents for the conversation are persisted (separate transactions, as above).
2. The conversation must exist, be yours and not be purging (404 otherwise).
3. An expired lease is first converted into an `interrupted` failure, so a replay after expiry shows the interrupted attempt. Then, if the `Idempotency-Key` was already used for a turn in this conversation: a different content
   hash (even whitespace) is **409 `idempotency_conflict`**; the same content **replays** the stored `Turn` (all attempts) as JSON, without invoking the model or re-attaching to a running stream. A
   replay returns the stored state even while the original attempt is still active.
4. If another attempt still holds an unexpired lease: **409 `conversation_busy`**. Concurrent submissions with different keys have exactly one winner.
5. Otherwise the turn, user message, pending assistant message and run are created and the
   conversation is leased for `turn_lease_seconds` (150 s). The lease is not renewed; the turn
   deadline is 120 s.

**Retry** (`turns/{turn_id}:retry`): after the same preliminary steps, a repeated retry key replays
the `Turn` (409 `idempotency_conflict` if its `turn_id` or `expected_run_id` differ); a busy
conversation is 409; and the request is **409 `stale_retry`** unless `expected_run_id` is the
conversation's newest run, belongs to `turn_id` and is `failed` (this includes retrying a turn that
has already completed). A valid retry creates a new run (attempt number + 1, `retry_of_run_id` set)
with a new assistant message, run ID and trace ID, reuses the user message and flips the turn back to
`active`.

Resending the *original* turn key after a failure only replays the stored `Turn`; retrying needs a
**fresh** key via the retry route.

A client that disconnects before the response body is read may leave the lease held until it expires
(up to 150 s), during which other submissions get `conversation_busy`; this is inferred from the
code and not covered by a test.

**Recovering after a disconnect:** read `GET …/turns/{turn_id}` (the most authoritative source; it
also drains buffered failure intents), or `GET …/messages` if the turn ID was never received. A
`failed` stream event is not proof the answer failed: if a completion commit succeeded but its
acknowledgement was lost, the stream says `failed` with `retry_available=false` while the stored turn
is `completed`.

### Feedback

- **Message feedback** targets a `completed` assistant message in an owned, active conversation
  (otherwise 404, or 422 `feedback_target` for a non-assistant or non-completed message). `PUT` needs a
  rating. The comment is stored only for `dislike`; a `like` silently clears any earlier comment.
  `PUT` replaces the whole record (upsert, keeping `created_at`). `DELETE` is idempotent. Clients
  cannot supply a run ID: attribution to the run and trace is joined from history.
- **Conversation feedback** is a full overwrite per user and conversation (a comment-only `PUT` nulls
  an earlier rating) and records the newest run and message order at the time. It may be submitted
  while a turn is active.

### Chat errors

| Status | `title` / code | Cause |
|---|---|---|
| 401 | `invalid_identity` | Missing, malformed, forged, expired, oversized (> 16384 chars) or wrong-audience token |
| 503 | `identity_unavailable` | Google's public certificates could not be fetched |
| 404 | `conversation_not_found` | Conversation, message or turn missing, not yours, or purging |
| 409 | `conversation_busy` | Another attempt holds the lease |
| 409 | `idempotency_conflict` | Key reused with different content (or different retry target) |
| 409 | `stale_retry` | `expected_run_id` is not the failed newest attempt of the turn, or `turn_id` is not the turn of the conversation's newest run (an unknown `turn_id` in a conversation that has attempts is 409, not 404) |
| 422 | `feedback_target` | Feedback on a missing-rating or non-completed/non-assistant target |
| 422 | `invalid_input` / `request_rejected` | Input rejected by the conversation store or a port |
| 500 | `internal_error` | Data-integrity failure or unhandled error |
| 503 | `service_unavailable` | Database or checkpoint store unavailable |

After the response has started, errors cannot change the HTTP status: they arrive as a `failed` event (category `internal` if unmapped). The connection ends with no terminal event only on disconnect, process death, or an error in the failure handling itself. `Retry-After` handling exists in the handler but no code path sets it.

### Examples

These assume `CHAT_URL` and a valid `GOOGLE_ID_TOKEN` (see [Authentication](../guides/authentication.md)).

```zsh
CHAT_URL=http://localhost:8080
curl -fsS -X POST "$CHAT_URL/v1/conversations" \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN"
# 201 {"id": "<conversation-uuid>", "status": "active", "title": "New conversation", ...}
```

```zsh
CONVERSATION_ID='<uuid-from-the-previous-response>'   # substitute
curl -N -sS -X POST "$CHAT_URL/v1/conversations/$CONVERSATION_ID/turns:stream" \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"content": "Summarise the risk management approach in the handbook."}'
# Expect events: started, progress…, delta…, sources, completed
```

## Ingestion API

### Routes

| Method and path | Request | Success | Domain errors |
|---|---|---|---|
| `POST /v1/documents` | header `Idempotency-Key`; multipart (below) | **202** when the job is queued/processing/retrying; **200** when a replayed job is already ready/failed/cancelled. Both carry a `Location` header → `AcceptanceBody` | 401, 404, 408, 409, 413, 422, 503 |
| `GET /v1/documents` | `limit` 1–100 (default 50), `offset` ≥ 0 (default 0) | 200 JSON **array** of `JobStatusBody` | 401, 503 |
| `GET /v1/documents/{document_id}` | | 200 `JobStatusBody` of the document's newest index or delete job | 404, 401, 503 |
| `GET /v1/ingestion-jobs/{job_id}` | | 200 `JobStatusBody` | 404, 401, 503 |
| `POST /v1/ingestion-jobs/{job_id}:retry` | none | 200 if the job is already `ready` (no-op), otherwise 202 → `JobStatusBody` | 404, 409, 401, 503 |
| `DELETE /v1/documents/{document_id}` | none | 202 (`deleting`) or 200 (`deleted`) → `DeletionBody` | 404, 401, 503 |

List semantics: no total or envelope; ordered by document creation time descending; **includes
`deleted` tombstones** (filename null). `GET /v1/documents/{id}` returns the newest index or delete
job, so while a replacement is in flight it reflects the replacement, and after a failed replacement it
reports `failed` even though the previous version stays searchable.

### Upload

`multipart/form-data` with these parts:

| Part | Required | Rule |
|---|---|---|
| `file` | yes, exactly one | `.pdf` with content type exactly `application/pdf`, or `.docx` with exactly `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (extension matched case-insensitively; a content-type parameter such as `; charset` is a mismatch). Name ≤ 255 characters (directory part ignored), no control characters. At most 50 MiB (`max_upload_bytes`, enforced on the actual stream; the whole request is capped at that plus 64 KiB). Legacy `.doc`, empty, encrypted, malformed or oversize-when-expanded files are rejected |
| `metadata` | no (default `{}`) | JSON text ≤ 16 KiB, unknown keys rejected: `document_id` (UUID or null), `corpus` (default `""`, single line, no control characters), `project_metadata` (object; single-line keys; values may contain tab and newline but no other control character) |
| `document_id` | no | UUID of an owned, live document to replace. Give it here **or** in `metadata`, not both |

At most one file and two form fields, each text part ≤ 16 KiB; violating these returns 400 with a string
`detail`.

Validation order: `Idempotency-Key` header (default 422) → authentication → form limits (400) → `file` is a file part and `metadata`, if sent, a text part (`file_and_metadata_required`) → metadata JSON and characters
(`invalid_upload_metadata`) → `document_id` (`invalid_document_id`) → filename (`invalid_filename`) →
extension/length (`unsupported_file`) → content type (`file_type_mismatch`) → size (413) and emptiness
(`empty_file`) → structural parse in an isolated process (`parser_timeout`, `malformed_file`,
`parser_failed`). Nothing is stored for any rejection above. The whole request has a 120 s deadline (408
`upload_timeout`).

`AcceptanceBody`: `document_id`, `version_id`, `job_id`, `status`, `deduplicated`, `retry_available`,
`status_url`, `retry_url` (null unless retry is available).

Replay and deduplication rules (one result per key, same content resolves to the existing job) are in
[Ingestion service](../services/ingestion.md#acceptance-api).

### Status models

`JobStatusBody`: `document_id`, `filename` (null after cleanup), `version_id`, `job_id`, `status`
(`queued`, `processing`, `retrying`, `ready`, `failed`, `cancelled`), `stage` (`extraction`,
`embedding`, `publication`, `cleanup`, `done`), `lifecycle` (`live`, `deleting`, `deleted`),
`total_chunks` (null until the full manifest is persisted), `completed_chunks`, `retrying_chunks`,
`failed_chunks`, `attempts`, `retry_cycle`, `error_category`, `created_at`, `updated_at`,
`retry_available`, `retry_url`, `status_url`.

`DeletionBody`: `document_id`, `lifecycle`, `status_url` (the document URL, not a job URL).

`error_category` values: `storage_unavailable`, `provider_unavailable`, `provider_protocol`,
`provider_rejected`, `extraction_failed`, `no_extractable_text`, `manifest_incomplete`,
`integrity_failed`, `budget_exhausted`, `internal`.

### Retry and delete semantics

- **`:retry`** applies only to a `failed` **index** job of a live, unretired version. The job is
  re-queued in place; completed vectors are kept and only unfinished chunks are embedded again. A job
  that is not failed is returned unchanged. 409 when the document is deleting/deleted, the version is
  retired, the job is not an index job (delete and superseded-cleanup jobs cannot be retried this way),
  or another index job is active on the document.
- **`DELETE`** immediately removes the document from retrieval, then cleanup runs asynchronously; poll
  `status_url` until `lifecycle` is `deleted`. Repeating it is safe: it restarts a failed cleanup job
  without creating a second one. Behaviour in detail: [Ingestion service](../services/ingestion.md#deletion).

### Ingestion errors

| Status | `detail` | Cause |
|---|---|---|
| 401 | `invalid_identity` | Missing, invalid or wrong-audience token |
| 404 | `not_found` | Resource absent or owned by another subject |
| 408 | `upload_timeout` | Upload exceeded 120 s |
| 409 | `request_conflict` | Key reused with different content; content already belongs to another document; target document not live; replacement already in progress; retry unavailable. **The reason is not exposed in the body** |
| 413 | `file_too_large` | File or request body over the limit |
| 422 | one of the upload codes above | Upload validation failure |
| 422 | `request_rejected` | Database rejected the data |
| 500 | `internal_error` | Integrity failure or unhandled error |
| 503 | `service_unavailable` | Database, storage or identity dependency unavailable; also `upload_finalization_expired` and a bucket that is not versioned |

### Example

```zsh
INGESTION_URL=http://localhost:8081
curl -sS -X POST "$INGESTION_URL/v1/documents" \
  -H "Authorization: Bearer $GOOGLE_ID_TOKEN" \
  -H "Idempotency-Key: upload-example-1" \
  -F 'file=@proposal.pdf;type=application/pdf' \
  -F 'metadata={"corpus":"projects","project_metadata":{"project":"Horizon"}}'
# 202 {"document_id": "...", "job_id": "...", "status": "queued", "status_url": "/v1/ingestion-jobs/<job_id>", ...}
```

Poll `status_url` (the job URL; prefix it with `$INGESTION_URL`), or `GET /v1/documents/{document_id}`, until `status` is `ready` or `failed`
([Smoke test](../guides/smoke-test.md) has a bounded polling loop).

## Health and readiness

| Service | `GET /health` | `GET /ready` |
|---|---|---|
| Chat | 200 `{"status":"alive"}`; 503 `{"status":"stopped"}` only if a background loop crashed. Database outages never fail it | 200 `{"status":"ready"}` / 503 `{"status":"unready"}`: requires live loops **and** the application revision equal to the image's, the chunk `embedding` column typed `vector(1024)`, no live published chunk with a mismatched model/dimension, null embedding, blank filename/text, a type other than pdf/docx or invalid locators, **and** the checkpoint schema at the expected revision. Bounded by `readiness_timeout_seconds` (5 s) |
| Ingestion | always 200 `{"status":"alive"}` | 200 / 503 as above: `app.alembic_version` equals the image's revision **and** the configured embedding dimension is 1024. No MinIO, Bedrock, worker or pgvector check |

Neither calls AWS or Google. A runtime image whose schema revision differs from the database, older
**or** newer, is unready, so rolling deployments lose readiness on old replicas as soon as the
database is upgraded ([Deployment](../guides/deployment.md)). Readiness succeeds with an empty index.

## Implementation references

[`services/chat/src/horizon_chat/api/routers/`](../../services/chat/src/horizon_chat/api/routers),
[`api/exception_handlers.py`](../../services/chat/src/horizon_chat/api/exception_handlers.py),
[`domain/streaming.py`](../../services/chat/src/horizon_chat/domain/streaming.py),
[`services/ingestion/src/horizon_ingestion/api/`](../../services/ingestion/src/horizon_ingestion/api),
[`api/schemas.py`](../../services/ingestion/src/horizon_ingestion/api/schemas.py),
[`api/errors.py`](../../services/ingestion/src/horizon_ingestion/api/errors.py).
