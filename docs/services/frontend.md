# Frontend

The frontend is the browser workspace for Horizon Copilot: sign in with Google, chat with the
assistant, give feedback, and manage the documents that the assistant can cite. It is a React 19 /
Vite single-page application built to static files and served by nginx in its container image.

**Boundaries.** The frontend holds no secrets and makes no authorization decisions. It obtains a
Google ID token in the browser and sends it to the [chat](chat.md) and [ingestion](ingestion.md)
APIs, which verify it. It talks to both APIs directly from the browser, so each API must allow the
frontend's origin (see [CORS](../reference/api.md#conventions-shared-by-both-apis)).

## Source layout

All paths are under [`services/frontend`](../../services/frontend).

| Path | Responsibility |
|---|---|
| [`src/main.tsx`](../../services/frontend/src/main.tsx) | Mounts `<App/>` |
| [`src/app/App.tsx`](../../services/frontend/src/app/App.tsx) | Composition root: identity state, expiry timer, in-memory recovery stores, one `Api` client per backend |
| [`src/features/identity/`](../../services/frontend/src/features/identity) | Google Identity Services sign-in, ID-token parsing (`parseIdentity`) |
| [`src/features/chat/`](../../services/frontend/src/features/chat) | Conversation list, composer, transcript, streaming and reconciliation (`useChatSession.ts`, `stream.ts`) |
| [`src/features/documents/`](../../services/frontend/src/features/documents) | Upload, status polling, retry and delete dialog (`useDocuments.ts`) |
| [`src/features/feedback/`](../../services/frontend/src/features/feedback) | Like/dislike and comment control, injected into chat as a render prop |
| [`src/shared/`](../../services/frontend/src/shared) | `api.ts` (HTTP client, `ApiError`), `config.ts` (public runtime config), `Dialog.tsx` |
| [`public/config.js`](../../services/frontend/public/config.js) | Default runtime configuration for development and static hosting |
| [`nginx.conf`](../../services/frontend/nginx.conf), [`runtime-config.sh`](../../services/frontend/runtime-config.sh), [`Dockerfile`](../../services/frontend/Dockerfile) | Container image |

`App` imports every feature; features do not import each other (chat receives the feedback control
through a render prop).

## Runtime configuration

`index.html` loads `/config.js` before the application bundle. The script defines
`window.HORIZON_CONFIG`, which `readConfig()` in
[`src/shared/config.ts`](../../services/frontend/src/shared/config.ts) validates at start-up.

| Config key | Container variable | Default (static file / container) | Purpose |
|---|---|---|---|
| `googleClientId` | `FRONTEND_GOOGLE_CLIENT_ID` | empty | Public OAuth web client ID used for sign-in. Must equal the audience the APIs expect |
| `chatApiUrl` | `FRONTEND_CHAT_API_URL` | `http://localhost:8080` | Browser-reachable base URL of the chat API |
| `ingestionApiUrl` | `FRONTEND_INGESTION_API_URL` | `http://localhost:8081` | Browser-reachable base URL of the ingestion API |
| `localIdentity` | none (container always writes `false`) | `false` | Development-only sign-in without Google, see below |

In the container, `runtime-config.sh` runs from `/docker-entrypoint.d/` at start-up and writes
`/usr/share/nginx/html/config.js` from only those three variables, so one image serves any
environment. Compose passes `FRONTEND_GOOGLE_CLIENT_ID` from the root `.env` `GOOGLE_CLIENT_ID` and
hard-codes the two API URLs to `http://localhost:8080` and `http://localhost:8081`.

Start-up fails (blank page; there is no error boundary) when `window.HORIZON_CONFIG` is missing or
an API URL is not `http:`/`https:` or carries credentials, a query or a fragment. With an empty
`googleClientId` the sign-in screen shows "Configure the public Google client ID to sign in."

**Local identity** (`localIdentity: true`) signs in as the fixed subject `local-horizon-user`
without a token. `readConfig()` accepts it only when all of these hold: both API hostnames and the
page hostname are loopback (`localhost`, `127.0.0.1`, `[::1]`), and the app runs under `vite dev`
(`import.meta.env.DEV`). Production builds and `vite preview` throw. The APIs must separately run
with local identity (see [Authentication](../guides/authentication.md#local-identity-mode)).

## Authentication and session handling

- The Google Identity Services script is loaded from `https://accounts.google.com/gsi/client`.
  The sign-in callback receives an ID token (a JWT).
- `parseIdentity` decodes the token payload **without verifying the signature**; it only reads
  `sub` and `exp` to display state and schedule expiry. Signature and audience are verified by the
  APIs.
- The token lives only in React state and in the `Api` client. Nothing is written to
  `localStorage`, `sessionStorage`, cookies or IndexedDB, so a page reload requires signing in again.
- There is no token refresh. When `exp` passes, or any API call returns 401, the app signs the user
  out with "Your session expired. Sign in to recover your saved work." Pending-request recovery
  state survives expiry only for the same Google subject; signing in as a different subject, or
  signing out, clears it.
- Requests go to `${base}/v1<path>` with `Authorization: Bearer <token>`, `cache: "no-store"` and
  `credentials: "omit"`. The client does not retry automatically.
- `ApiError` maps statuses to fixed user messages (401 session expired, 403 no access, 409 conflict
  with saved work, 413/422 input rejected, everything else generic).

## Backend calls

Chat API (`chatApiUrl`):

| Call | Used for |
|---|---|
| `GET /v1/conversations?limit=50[&cursor=…]` | Conversation list |
| `POST /v1/conversations` | New conversation |
| `GET /v1/conversations/{id}`, `GET …/messages?cursor=N&limit=100`, `GET …/turns/{turn_id}` | Reconciliation and history (messages are read until `next_cursor` is null) |
| `POST /v1/conversations/{id}/turns:stream` | Send a question (SSE response, or JSON `Turn` on replay) |
| `POST /v1/conversations/{id}/turns/{turn_id}:retry` | Retry the failed latest attempt |
| `PUT`/`DELETE /v1/messages/{id}/feedback`, `PUT`/`DELETE /v1/conversations/{id}/feedback` | Answer and conversation feedback |

Ingestion API (`ingestionApiUrl`):

| Call | Used for |
|---|---|
| `GET /v1/documents?limit=100&offset=N` | Document list, paged until a short page; also the polling target while documents are active |
| `GET /v1/documents/{id}` | Canonical status once after an upload is accepted, and when recovering a delete whose response was lost |
| `POST /v1/documents` | Upload (multipart `file`, `Idempotency-Key`) |
| `POST /v1/ingestion-jobs/{job_id}:retry` | Retry a failed indexing or cleanup job |
| `DELETE /v1/documents/{id}` | Delete (also restarts failed cleanup) |

The frontend never calls the health routes or `GET /v1/ingestion-jobs/{id}`, and never sends the
upload `metadata` or `document_id` fields. Full contracts: [API reference](../reference/api.md).

## Chat behaviour

- **Send.** `send()` generates a UUID `Idempotency-Key`, stores `{key, content}` in an in-memory
  recovery map keyed by conversation, then POSTs. The `Content-Type` of the response decides what
  happened: `text/event-stream` is a new attempt; anything else is a replayed `Turn` JSON.
- **Streaming.** `decodeSse` in [`stream.ts`](../../services/frontend/src/features/chat/stream.ts)
  reads the `fetch` body (not `EventSource`) with a fatal UTF-8 decoder and handles LF, CRLF and
  lone CR across chunk boundaries. It validates every event's envelope, throws on a sequence gap,
  a changed `run_id` or an `event:` name that disagrees with the JSON `type`, and ignores duplicate
  sequences. An incomplete event at end of stream is dropped. Protocol: [Events](../reference/events.md#chat-turn-stream-sse).
- **Reconciliation.** If a stream ends without `completed` or `failed`, or the request fails
  ambiguously, the client reads the conversation, messages and turn status from the API
  ("Checking saved work…"). While a turn is active or a recovery entry exists it polls every 2 s,
  pausing when the tab is hidden. If the POST outcome is unknown (no `turn_id` yet), "Check submission outcome" re-sends the same key and relies on server replay.
- **Retry.** The **Retry answer** button appears only for a failed assistant message whose turn reports
  `retry_available` and whose run is the turn's latest. It sends a **fresh** key with
  `expected_run_id`.
- **Feedback.** Answer feedback is offered only for `completed` messages; it is separate from the
  conversation-level comment.

The client drops a recovery entry after a 4xx other than 401/409 (including 408 and 429).

## Documents behaviour

- Client-side validation accepts only non-empty `.pdf` and `.docx` files; size and format limits are
  enforced by the ingestion API.
- An upload stores `{key, file}` before the POST. A definitive 4xx rejection (not 401, 408, 429)
  clears it; network errors, 5xx, 401, 408 and 429 mark the upload "unknown" and **Retry same
  upload** re-sends the same key and file so the server deduplicates.
- Polling runs while any document is `queued`, `processing`, `retrying` or `deleting`. The delay
  is `min(30 s, 2 s × 2^min(failures, 4))`, paused while the tab is hidden.
- Delete marks the document as deleting locally, then follows server status until it disappears.

## Build and run

Commands run from `services/frontend` and need Node 24 (`engines.node` is `>=22.12`; CI and the
image use 24).

| Task | Command | Notes |
|---|---|---|
| Install | `npm ci` | Exact pins in `package-lock.json` |
| Dev server | `npm run dev` | Binds `127.0.0.1:3000` with `--strictPort` (stop the Compose `frontend` first); open it as `http://localhost:3000` so the origin matches the Google and CORS configuration; serves `public/config.js` as-is |
| Type check | `npm run typecheck` | |
| Lint | `npm run lint` | |
| Production build | `npm run build` | Runs `typecheck`, then `vite build` into `dist/` |
| Preview build | `npm run preview` | Binds `127.0.0.1:3000`, `--strictPort`; open it as `http://localhost:3000` |
| Unit tests | `npm test` | Vitest, `src/**/*.test.ts` |
| Browser tests | `npm run test:browser` | Playwright, see [Testing](../guides/testing.md#frontend) |
| Everything | `npm run check` | lint, build, unit tests, browser tests |

For local development, edit `public/config.js` with your public Google client ID and add
`http://localhost:3000` to the OAuth client's **Authorized JavaScript origins** in Google Cloud.
The APIs' `FRONTEND_ORIGIN` must equal the page origin exactly (no trailing slash).

## Container image and serving

[`nginx.conf`](../../services/frontend/nginx.conf) serves `/usr/share/nginx/html` on port 80:

| Location | Behaviour |
|---|---|
| `/health` | `200 healthy`, not logged (image and Compose health check) |
| `/config.js` | `Cache-Control: no-store` |
| `/index.html` | `Cache-Control: no-cache` |
| `/assets/` | `Cache-Control: public, max-age=31536000, immutable`; a missing file is a 404, not the SPA fallback |
| everything else | `try_files $uri $uri/ /index.html` (single-page-app fallback) |

Server-level `X-Content-Type-Options: nosniff` and `Referrer-Policy: strict-origin-when-cross-origin`
are declared, but nginx does not inherit server-level `add_header` into a location that declares its
own, so `/config.js`, `/index.html` and `/assets/` responses are expected to carry only their
`Cache-Control` header. This follows nginx's inheritance rule and has not been checked against a
running container. No CSP, HSTS or compression is configured.

In Compose the service is `frontend` (`127.0.0.1:3000` → container port 80). It has no
`depends_on`, so the sign-in screen can load while the APIs are still starting. The host port, API
URLs and both APIs' allowed origin are literals in [`compose.yaml`](../../compose.yaml); changing
them means editing the file (the APIs' `FRONTEND_ORIGIN` / `INGESTION_FRONTEND_ORIGIN` and the
Google authorized origin must change together).

## Deploying and rolling back

- Serve `dist/` (or the image) over HTTPS with SPA fallback to `index.html`. Keep `config.js`
  uncached so a per-environment config change takes effect without rebuilding JavaScript.
- Both API URLs must be reachable **from the browser**; container DNS names are not.
- A reverse proxy in front of the chat API must not buffer or compress the `POST …:stream`
  response, or text arrives in one lump. The chat API already sends `X-Accel-Buffering: no`.
- Deploy the frontend only after the APIs expose the `/v1` contracts it calls and the
  [migrations](migrations.md) are applied.
- Roll back by redeploying the previous static artifact or image **and its matching public
  configuration**. Frontend rollback needs no database change. Keep previous hashed assets
  available during a rolling release so pages loaded from older HTML still find their files.

## Implementation references

[`useChatSession.ts`](../../services/frontend/src/features/chat/useChatSession.ts),
[`stream.ts`](../../services/frontend/src/features/chat/stream.ts),
[`useDocuments.ts`](../../services/frontend/src/features/documents/useDocuments.ts),
[`api.ts`](../../services/frontend/src/shared/api.ts),
[`config.ts`](../../services/frontend/src/shared/config.ts),
[`identity.ts`](../../services/frontend/src/features/identity/identity.ts).
