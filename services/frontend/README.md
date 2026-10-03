# Horizon frontend

React/Vite SPA with owner-scoped Google ID-token authentication, durable chat
reconciliation, independent answer/conversation feedback, and document lifecycle
management. Feature modules
own identity, chat, documents, and feedback; `app/` composes them, while `shared/`
contains generic API/configuration/dialog primitives. Tokens and request recovery
metadata stay in memory. Sign-out clears both; expiration retains only same-owner
recovery metadata for reauthentication. A reload requires signing in again.

## Local development

Use Node 24 and npm. Dependencies are pinned in `package-lock.json`.

```sh
cd services/frontend
npm ci
npm run dev
```

The frontend origin is `http://localhost:3000`. Edit `public/config.js` with the
public Google OAuth web client ID and browser-accessible API URLs. Add the exact
frontend origin to the client's Authorized JavaScript origins in Google Cloud.
Use the same audience in both APIs (`GOOGLE_CLIENT_ID` and
`INGESTION_GOOGLE_CLIENT_ID`). Google sign-in follows the
[Google Identity Services JavaScript contract](https://developers.google.com/identity/gsi/web/reference/js-reference).
No OAuth client secret, AWS credential, DB connection, or object-store credential
belongs in this file. Keep it out of application logs.

Compose includes the `frontend` service on `FRONTEND_PORT` (default 3000), with
public browser API URLs derived from `CHAT_PORT` / `INGESTION_PORT`. It starts
independently so the sign-in surface can load while APIs are starting. Run
`docker compose up --build -d frontend` after configuring the root `.env`.
Changing `FRONTEND_PORT` also changes both APIs' exact CORS origin; restart them
and add the new origin in Google Cloud.

Both APIs already allow only their configured `FRONTEND_ORIGIN` /
`INGESTION_FRONTEND_ORIGIN`, with Authorization, Content-Type, and
Idempotency-Key headers. Set them to the exact frontend origin, without a trailing
slash. The frontend calls the public APIs directly; container DNS names are not
browser addresses. Local identity can be enabled explicitly in `public/config.js`
only under Vite development on loopback, with both APIs using their existing
loopback-bound local identity configuration. Production assets refuse that mode.

## Quality checks

```sh
npm run typecheck
npm run lint
npm test
npx playwright install chromium
npm run test:browser
```

Browser tests build and serve production assets on port 3002, using Google and API
contract fixtures. Set `FRONTEND_TEST_PORT=3003` if port 3002 is already occupied.
They cover streaming/reconciliation/retry, separate feedback,
upload-to-Ready, deletion, account isolation, and recovery after authentication
expires. A real HTTP stream test verifies incremental rendering and scroll
behavior. They do not obtain real Google tokens or invoke AWS. Decoder tests cover
adversarial UTF-8/frame boundaries and terminal/sequence invariants.

Run the backend integration profile against disposable PostgreSQL and private
versioned MinIO to verify durable feedback-to-run joins, canonical filenames,
PDF/.docx upload-to-cited-chat, retries, cleanup, and owner isolation:

```sh
# From the repository root, with TEST_DATABASE_DSN and TEST_MINIO_ENDPOINT configured:
REQUIRE_INTEGRATION=1 uv run --locked pytest services/chat/tests/integration services/ingestion/tests/integration
```

Browser fixtures and database integration tests provide separate layers of
verification; browser mocks cannot prove durable database joins. Complete the
signed-in smoke workflow below with real Google/AWS configuration before deploying
to users. Accessibility checks use axe plus keyboard focus, narrow viewport, and
reduced-motion scenarios.

## Static deployment and rollback

```sh
npm run build
npm run preview
# Or from the repository root:
docker build -f services/frontend/Dockerfile -t horizon-frontend:local .
```

Deploy `dist/` to a static host with SPA fallback to `index.html`. Serve `config.js`
with `Cache-Control: no-store`, HTML with revalidation, and hashed assets with
immutable caching. Replace `config.js` per environment without rebuilding JS.
The container generates that file at startup from `FRONTEND_GOOGLE_CLIENT_ID`,
`FRONTEND_CHAT_API_URL`, and `FRONTEND_INGESTION_API_URL`; it never copies the
container environment wholesale. Runtime JSON encoding preserves literal strings.
Use HTTPS for deployed origins and APIs, then update both APIs' exact CORS origin
and Google's Authorized JavaScript origins. The browser must be able to reach both
API URLs. A reverse proxy in front of chat must not buffer or compress POST SSE.

The APIs must provide the revised `/v1` conversation/history/turn/retry and
message/conversation feedback contracts, plus ingestion acceptance/status/retry/
deletion, with migrations applied before the frontend release. A persisted SSE
`completed` event or completed authoritative history enables answer feedback.
Network loss triggers reconciliation; deliberate retry uses the original turn and
a fresh key. Unknown acceptance retains its original request key.

Ingestion list/document/job/retry status must include the persisted nullable
`filename`. Index jobs identify the candidate version; deletion status retains the
document name until cleanup and returns null for deleted tombstones. This additive
projection needs no new database migration. Repeated DELETE must safely restart a
failed cleanup job so the panel's Retry cleanup action can finish deletion.

Before release, sign in, upload a PDF or .docx, follow publication to Ready, ask a
question, verify text arrives before completion and citations are available, rate
an answer, leave a separate conversation comment, reopen the conversation, retry
a failed answer, delete the named document through Deleting to Deleted, and confirm
old citations report unavailable. Repeat with another account and on mobile.

Roll back by restoring a prior static artifact/image and its matching public
configuration. Preserve existing database records and compatible API contracts;
frontend rollback requires no reverse migrations. Keep prior hashed assets during
rolling releases so clients with old HTML can still load them.
