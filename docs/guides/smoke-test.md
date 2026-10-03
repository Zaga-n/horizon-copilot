# Smoke test

Checks that a running system works, in three layers: stack readiness (no credentials), a scripted API
walk-through (needs a token or local identity, and AWS), and the manual signed-in browser workflow.
Readiness is not success: a ready stack can still fail every chat turn because of AWS or Google
configuration.

The scripted steps were assembled from the API contracts and checked against the code; they were **not
executed** during documentation because that needs real Google and AWS credentials.

## Prerequisites

- A running stack: [Local deployment](local-deployment.md) (or a deployed environment with its URLs).
- For layers 2 and 3: valid AWS credentials with Bedrock access configured in the stack, and either a Google
  ID token ([Authentication](authentication.md#getting-a-token-for-api-testing)) or APIs running in local
  identity mode.
- `curl`, `jq` and `uuidgen` on the host for the scripted layer (`jq` and `uuidgen` are assumed installed).
- Run from the repository root (the sample file path is relative).

## Layer 1: stack readiness (no credentials)

```zsh
curl -fsS http://localhost:8080/ready      # {"status":"ready"}
curl -fsS http://localhost:8081/ready      # {"status":"ready"}
curl -fsS http://localhost:13133/          # Collector health
docker compose ps -a                       # one-shot jobs exited 0, services healthy
docker compose exec ingestion-worker python -m horizon_ingestion.main worker-health && echo worker healthy
```

Optional: verify database role boundaries and the telemetry route
([Local deployment](local-deployment.md#optional-checks),
[Observability](../operations/observability.md#verifying-the-pipeline)). A 503 from `/ready` points to a
schema or database problem ([Troubleshooting](../operations/troubleshooting.md)).

## Layer 2: scripted walk-through

Variables (substitute the token; with local identity mode the `Authorization` header is ignored, but keep
the variable defined):

```zsh
CHAT_URL=http://localhost:8080
INGESTION_URL=http://localhost:8081
GOOGLE_ID_TOKEN='<paste-id-token>'    # manual input; see Authentication
AUTH_HEADER="Authorization: Bearer $GOOGLE_ID_TOKEN"
```

### 1. Upload a document and follow it to `ready`

```zsh
UPLOAD_KEY="smoke-$(uuidgen)"
ACCEPT=$(curl -fsS -X POST "$INGESTION_URL/v1/documents" \
  -H "$AUTH_HEADER" -H "Idempotency-Key: $UPLOAD_KEY" \
  -F 'file=@dev/data/9.2-Project-management-Handbook.pdf;type=application/pdf' \
  -F 'metadata={"corpus":"smoke"}')
printf '%s\n' "$ACCEPT" | jq '{document_id, job_id, status, deduplicated, status_url}'
DOCUMENT_ID=$(printf '%s\n' "$ACCEPT" | jq -r .document_id)
```

Expected: HTTP 202 and `status` of `queued` (or `processing`), `deduplicated: false`. Repeating the same
command **with the same `UPLOAD_KEY`** returns the same `document_id` with `deduplicated: true` and does not
store the file again; a different key with identical bytes, the same `corpus` and a live version resolves to the existing document too.

Poll to a deadline (10 minutes is a choice, not a documented limit; indexing time depends on file size and
Bedrock throughput):

```zsh
deadline=$((SECONDS + 600)); status=""
while (( SECONDS < deadline )); do
  doc=$(curl -fsS -H "$AUTH_HEADER" "$INGESTION_URL/v1/documents/$DOCUMENT_ID")
  status=$(printf '%s\n' "$doc" | jq -r '.status // empty')
  printf '%s\n' "$doc" | jq -c '{status, stage, total_chunks, completed_chunks, error_category}'
  [[ $status == ready || $status == failed ]] && break
  sleep 5
done
[[ $status == ready ]] || { echo "indexing did not reach ready (last status: ${status:-none})" >&2; false; }
```

Expected: `stage` moves `extraction` → `embedding` (with `completed_chunks` rising to `total_chunks`) →
`publication`, ending `status: ready`. On `failed`, read `error_category`
([Ingestion](../services/ingestion.md#retry-budgets)); for a transient category retry with
`curl -fsS -X POST -H "$AUTH_HEADER" "$INGESTION_URL/v1/ingestion-jobs/<job_id>:retry"` and poll again.

### 2. Ask a question and capture IDs

```zsh
CONVERSATION_ID=$(curl -fsS -X POST "$CHAT_URL/v1/conversations" -H "$AUTH_HEADER" | jq -r .id)

TURN_KEY="smoke-turn-$(uuidgen)"
curl -N -sS -X POST "$CHAT_URL/v1/conversations/$CONVERSATION_ID/turns:stream" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -H "Idempotency-Key: $TURN_KEY" \
  -d '{"content": "What does the handbook say about managing project risks?"}' | tee turn.sse
```

Expected stream ([Events](../reference/events.md#chat-turn-stream-sse)): `started`, one or more
`progress`, many `delta`, `sources`, then `completed`, and no `failed`. Assert and capture:

```zsh
grep -q '^event: completed' turn.sse && ! grep -q '^event: failed' turn.sse \
  || { echo "turn did not complete; see turn.sse" >&2; false; }
FIRST_EVENT=$(sed -n 's/^data: //p' turn.sse | head -1)
TURN_ID=$(printf '%s\n' "$FIRST_EVENT" | jq -r .turn_id)
ASSISTANT_MESSAGE_ID=$(printf '%s\n' "$FIRST_EVENT" | jq -r .assistant_message_id)
echo "trace_id: $(printf '%s\n' "$FIRST_EVENT" | jq -r .trace_id)"
```

The captured `trace_id` can be looked up in Tempo and Langfuse
([Observability](../operations/observability.md#correlating-one-request)). Then check the saved state:

```zsh
curl -fsS -H "$AUTH_HEADER" "$CHAT_URL/v1/conversations/$CONVERSATION_ID/turns/$TURN_ID" | jq '{status, retry_available}'
curl -fsS -H "$AUTH_HEADER" "$CHAT_URL/v1/conversations/$CONVERSATION_ID/messages" \
  | jq '.messages[] | {role, status, sources: [.sources[]? | {marker, filename, page, unavailable}]}'
```

Expected: turn `status: completed`; the assistant message `completed` with at least one source whose
`filename` is the uploaded file and `unavailable: false`. If the model answers without citing (for
example the guardrail declined the question), `sources` is empty.

**Idempotent replay.** Repeat the identical POST with the same `TURN_KEY`:

```zsh
curl -sS -D - -o /dev/null -X POST "$CHAT_URL/v1/conversations/$CONVERSATION_ID/turns:stream" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -H "Idempotency-Key: $TURN_KEY" \
  -d '{"content": "What does the handbook say about managing project risks?"}' | grep -i '^content-type'
```

Expected `content-type: application/json` (the stored `Turn`, no model call). The same key with different
text returns 409.

### 3. Feedback

```zsh
curl -sS -o /dev/null -w "%{http_code}\n" -X PUT "$CHAT_URL/v1/messages/$ASSISTANT_MESSAGE_ID/feedback" \
  -H "$AUTH_HEADER" -H "Content-Type: application/json" -d '{"rating": "like"}'
# 204
```

### 4. Negative and empty cases

```zsh
# An unsupported file type is rejected before anything is stored: 422 {"detail":"unsupported_file"}
curl -sS -w "\n%{http_code}\n" -X POST "$INGESTION_URL/v1/documents" \
  -H "$AUTH_HEADER" -H "Idempotency-Key: smoke-neg-$(uuidgen)" \
  -F 'file=@README.md;type=text/markdown' -F 'metadata={}'
```

For the empty-index case, ask a general in-scope question before uploading anything: readiness succeeds with
an empty index, and project-specific answers must disclose that evidence is absent. An image-only PDF
passes upload validation but ends `failed` with `no_extractable_text`.

### 5. Delete and confirm cleanup

```zsh
curl -fsS -X DELETE -H "$AUTH_HEADER" "$INGESTION_URL/v1/documents/$DOCUMENT_ID" | jq .
deadline=$((SECONDS + 180)); lifecycle=""
while (( SECONDS < deadline )); do
  lifecycle=$(curl -fsS -H "$AUTH_HEADER" "$INGESTION_URL/v1/documents/$DOCUMENT_ID" | jq -r '.lifecycle // empty')
  echo "lifecycle: $lifecycle"
  [[ $lifecycle == deleted ]] && break
  sleep 3
done
[[ $lifecycle == deleted ]] || { echo "cleanup did not finish (last: ${lifecycle:-none}); repeat the DELETE to restart it" >&2; false; }
```

Expected: 202 with `lifecycle: deleting`, then `deleted`. Re-run the history query from step 2: the old
citation now reports `unavailable: true`.

### Repeat-run effects and cleanup

- Re-running the upload before deleting resolves to the existing document (`deduplicated: true`); after
  deleting it creates a new document.
- Each run creates a conversation. There is **no API to delete a conversation**; it is removed by retention
  after 30 days of inactivity, or by resetting the local volumes.
- Remove the local capture file with `rm -f turn.sse`.

## Layer 3: signed-in browser workflow

Before releasing to users, with real Google and AWS configuration (Chrome or any browser, then a mobile
viewport and a second account):

1. Open the frontend and sign in with Google.
2. Upload a PDF or `.docx` and follow it to **Ready**.
3. Ask a question: text should arrive before completion, and citations should be available.
4. Rate the answer and leave a separate conversation comment; reload, sign in again and reopen the
   conversation: history, rating and comment persist.
5. Force or find a failed answer and use **Retry answer**; it creates a new attempt.
6. Delete the document and watch it go through **Deleting** until it disappears from the list; old citations report unavailable.
7. Repeat with another Google account: it must see none of the first account's documents or conversations.

Automated browser tests cover these flows against mocked contracts only
([Testing](testing.md#frontend)); they cannot show real Google or Bedrock behaviour.
