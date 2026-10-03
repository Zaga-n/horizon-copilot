## 1. Foundation and identity

- [x] 1.1 Create the TypeScript React/Vite package, feature boundaries, public configuration, and quality commands; verify install, typecheck, lint, and production build.
- [x] 1.2 Implement Google sign-in, memory-held ID tokens, API authorization, expiry handling, and sign-out/account cache isolation; verify login, denied access, and account-switch scenarios.
- [x] 1.3 Add local frontend launch/OAuth origin documentation and exact-origin CORS configuration for both APIs; verify a browser can call protected chat and ingestion endpoints without exposing service secrets.

## 2. Workspace and history

- [x] 2.1 Build responsive sidebar, chat header/composer, document panel/drawers, and visual tokens; verify desktop/mobile layouts, empty/error states, keyboard focus, contrast, and reduced motion.
- [x] 2.2 Implement create/list/select conversations and cursor-paginated history with server IDs/statuses; verify reopening a conversation restores message order, citations, and saved feedback.

## 3. Streaming and recovery

- [x] 3.1 Implement incremental POST SSE decoding and typed event handling; verify split UTF-8/event boundaries, multiple events per read, sequence handling, and single terminal transitions.
- [x] 3.2 Render incremental safe markdown, separate progress, source references, and user-controlled scrolling; verify visible deltas before completion and no forced scroll when reading old messages.
- [x] 3.3 Implement unknown-outcome reconciliation, active-turn blocking, failed-attempt rendering, and explicit retry with request keys; verify lost completion, partial failure, reconnect, and retry without duplicate user messages.

## 4. Feedback

- [x] 4.1 Add completed-answer like/dislike, optional dislike comment, edit/clear, pending/error states, and draft retention; verify dismiss preserves dislike and rating updates target the original assistant message/run.
- [x] 4.2 Add separate header conversation rating/comment controls including text-only input; verify persistence, edit/delete, and distinction from answer feedback after reload.

## 5. Documents

- [x] 5.1 Add selection/drop upload, validation presentation, transfer/acceptance states, and idempotent transport recovery; verify PDF/.docx, rejected inputs, and lost acknowledgment.
- [x] 5.2 Add lifecycle-aware polling, stage spinner, unknown totals, committed fractions, publication, and failed-ingestion retry; verify null total, 3/155, 155/155 publishing, 107/111 retry, and reload recovery.
- [x] 5.3 Add named delete confirmation and deleting/deleted reconciliation; verify Ready-document deletion, in-flight ingestion deletion, delayed cleanup, and old unavailable citations.

## 6. Integrated release verification

- [x] 6.1 Run browser integration tests covering authenticated streaming/retry, answer/thread feedback, upload-to-ready, and deletion against revised API contracts; verify cross-account isolation and durable feedback-to-run joins.
- [x] 6.2 Document static deployment, runtime public configuration, rollback, and API prerequisites; verify a production build served locally completes the core workflow and passes an accessibility review.
