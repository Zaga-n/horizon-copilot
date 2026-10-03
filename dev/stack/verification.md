# Current acceptance verification — 3 October 2026

All 15 tasks in `local-observability-stack` are complete. The remaining four
acceptance tasks were verified in the isolated `horizon-acceptance-20261003`
Compose project with fresh volumes, alternate loopback ports, Docker Engine
29.1.3 and Compose 5.0.0. Real AWS Bedrock Terra/Luna and Titan V2 were used.
Both APIs used explicit local identity bound to container loopback; requests
ran inside the API containers. No Google OAuth login or frontend was exercised.
The default deployment retains its Google bearer configuration.

Detailed PASS observations and execution identifiers are in
[acceptance-2026-10-03.json](acceptance-2026-10-03.json).

| Acceptance boundary | Evidence |
| --- | --- |
| Startup and storage | Fresh migrations and runtime grants completed before API readiness. MinIO versioning is Enabled; anonymous bucket listing returns 403. |
| Upload and replay | Real DOCX produced eight chunks. With the worker stopped, the accepted job stayed queued; replay returned the same job/document. Startup recovery completed it without its original notification. Status polling observed progress. |
| Listener and worker health | Terminating the dedicated LISTEN connection caused reconnect. The probe reports listener connectivity and recent successful recovery scans separately; a disconnected listener does not make a scanning worker unhealthy. |
| Selective retry | A temporary SDK fault injected `ThrottlingException` after one real Titan embedding, with finite retry budgets reduced for this fixture. One chunk remained completed and seven failed. After removing the fault, explicit retry completed all eight; the completed vector and its attempt count were unchanged. |
| Chat and restart | Real empty-index, cited and checkpoint follow-up turns completed. Runtime restart preserved history. Final generation uses only current-turn evidence/markers; missing current sources cannot legitimize historical citations. |
| Cleanup and catch-up | Both sample documents reached deleted through durable cleanup and their chunks were removed. Aging an isolated conversation by 31 days and restarting chat removed its history/checkpoints while preserving document records. |
| Rated retry | A temporary invalid main model ID produced a failed persisted attempt. Restoring the real profile produced a completed, streamed retry, then a like through the feedback API. The shipped SQL join executed successfully. |
| Telemetry | Real traces appear in Tempo and Langfuse with matching IDs, closed parent trees, correlated Loki logs and exactly one generation per physical model span. All 16 dashboard metric queries resolve; no-event request error/retry counters show zero. |
| Privacy | AWS credential canaries, private document text and hidden prompt content are absent from the inspected exported traces and correlated logs. Langfuse input/output are null by default. Feedback remains in PostgreSQL and is absent from trace metadata. |

The final cited trace is `b816857daa28e71a130ed65b4efdbf10`; the rated retry
trace is `d741ad015b070a61fde298b0aa168245`. Each has nine Tempo spans and nine
Langfuse observations, including five model generations with matching span IDs.
The failed provider attempt has trace `9c0194a59aec7825a7e933d0153df4d1`.
All three share conversation/session `601762a1-8826-4469-a3da-4392b2e44d8c`.
The failed and retry executions share turn
`76e8692e-ea66-4225-87f8-a843a9189206` and user message
`d080e04d-9637-4ac9-94ff-bc6dea1be6a8`, with distinct run and assistant IDs.
Only the retry has the stored like. The report records these joins directly.

Measured idle memory was 3278 MiB (approximately 3.2 GiB) across 14 running
containers, excluding builds, other local projects and peak parser/job load.
The runbook's 12 GiB / four CPU recommendation remains headroom, not a measured
minimum. Parser children now have a finite 1 GiB virtual-memory budget: their
imported virtual address space already exceeds the previous 512 MiB limit on
Linux, which otherwise rejected a valid DOCX as `malformed_file`.

Verification after changes: `uv run --locked pytest -m "not live"` passed
471 tests against disposable PostgreSQL/MinIO and the migration image, with no
skips. Ruff and per-member strict mypy pass; all 25 import contracts are kept.
The worker healthcheck was also executed in its deployed container.

The disposable acceptance project and its volumes are removed after verification.
Trace IDs are recorded evidence, not persistent live links. Existing Docker
projects and their data were not reset. Historical reports below document earlier
boundaries and pending status; this acceptance supersedes their pending lists.

---

# Previous deployment verification — 2 October 2026

The current root Compose deployment uses `grafana/otel-lgtm:0.30.2` with
Prometheus OTLP metrics ingestion, default Langfuse startup, Compose development
credential defaults, and anonymous Grafana. The earlier standalone LGTM check
below is retained as historical evidence; its overlay commands and Mimir backend
are superseded.

Fresh volumes reached healthy PostgreSQL, MinIO, LGTM, Langfuse backing services,
and Langfuse web; migrations/grants completed and both APIs became ready. No
extra Compose files or profiles were used. Anonymous Grafana dashboard/datasource
queries succeed. All 16 dashboard metric queries return data from Prometheus.
Routing Collector configuration validates on the pinned 0.159.0 image.

Chat traces `fd7086871d05408ea0d4597b66eb42db` and
`70e334327dc642bcb1594b26295a5d21` each have six spans in Tempo and five in
Langfuse, with connected parents and private canary attributes removed. Each has
one correlated Loki log. Ingestion traces `5df91517bc024c009561d7a5b4b5af42`
and `b7d5ac9db67142b4b8b35932a53eaeb7` retain three spans in Tempo and the
job → embedding pair in Langfuse, also with correlated logs and redaction.

Stopping Langfuse web/worker leaves both APIs ready; subsequent canary traces
still reach Tempo and the Langfuse export queue reaches two while bounded retry
logs appear. Collector self-metrics reach Prometheus directly over OTLP outside
the application routing pipeline. Langfuse project authentication uses the same
Compose key defaults as the web seeding, through the basicauth extension.

The four pending acceptance tasks remain unchanged. Paid Bedrock, Google login,
and live upload/chat/rating scenarios were not executed. The disposable test
project is removed after verification; its IDs document the run, rather than
remaining live trace links.

---

# Previous standalone-stack verification — 2 October 2026

Verified with Docker Desktop 4.56.0 on ARM64 and Docker Compose 5.0.0 in the
isolated `horizon-stack-verification` project, using disposable credentials,
fresh assistant volumes, and alternate host ports. The disposable verification
project and its volumes were removed after the checks; the trace IDs below
record the verified run and are no longer queryable in that removed project. No Bedrock request or real
Google identity was used. All runtime images and backing services built/pulled;
Compose image references are pinned to tested registry digests.

| Check | Result |
| --- | --- |
| Compose models | Base and Langfuse overlay validate |
| Collector models | Both validate with contrib 0.159.0 |
| Fresh PostgreSQL/MinIO | Healthy; private versioned bucket/account provisioned |
| Schema access | Migrators can create only in their schema; runtime DDL denied; ingestion chat/checkpoint access denied; chat indexing writes denied |
| Ordered startup | Application migrations → checkpoint setup → grants → runtimes; both APIs return ready with an empty index |
| Repeated setup/restart | Migration job succeeds again; persistent database/object store restart and APIs recover |
| Deliberate checkpoint identity failure | Migration exits 1; chat/API/worker containers remain Created and never start; corrected configuration restores startup |
| Langfuse deployment | UI health reports v3.160.0; authenticated OTLP ingress stores canaries |
| Grafana | Provisioned datasources and both dashboards; all 16 metric queries return data |
| Logs | One Alloy/Loki JSON canary record per trace; service labels are strings; provisioned derived link resolves `${__value.raw}` to Tempo |
| Collector self-metrics | Mimir receives uptime, receiver/exporter and queue metrics through independent direct OTLP |
| Langfuse outage | APIs remain ready; new complete traces appear in Tempo; Langfuse queue reaches 2 and logs show bounded retries without content/auth values |
| Database backup | `pg_dump -Fc` creates an archive; `pg_restore --list` reads its contents |
| Python checks | Ruff and strict mypy pass for the smoke CLI |
| Offline tests | 309 passed in 35.80s, including real PostgreSQL/MinIO integration and migration container tests; paid model boundaries controlled |
| OpenSpec | Strict validation passes |

Synthetic chat trace `ba049a1d9138406b858fc05815d734ac` contains six spans in
Tempo and five in Langfuse. The latter retains the root, agent, retrieval,
embedding and exactly one generation; it drops the operational persistence
sibling. Every retained span ID matches Tempo, and every retained parent is
present. The retry trace `61a5ee4491d8418db60a6521dc734eee` has the same shape.
Both share conversation/session `291cbaaf-a805-4d48-a9b6-eb3cba385e1a`, while
run IDs, assistant message IDs, attempt numbers and trace IDs remain distinct.
Both branches preserve prompt/agent/retrieval versions and bounded chunk/version
reference canaries. Fake authorization, private input and status-message values
are absent from both destinations. Synthetic ingestion job traces similarly
retain job → embedding in Langfuse and the publication sibling in Tempo.

Measured idle memory for this full verification stack was approximately 3.2 GiB
across its containers; this excludes image builds, test processes, and model/job
load. Docker had 7.65 GiB available. The runbook's larger allocation recommendations
allow startup/build/load headroom; they are not measured minimum requirements.

## Pending acceptance

OpenSpec tasks 1.4, 4.1, 4.2 and 5.2 remain unchecked. Worker configuration and
recovery supervision are present, but the existing application exposes no
listener/recovery-scan health probe. Adding that probe changes application code
outside the change's stated packaging scope, so a scope decision is pending.

Live direct upload, lost-response replay, selective retry, deletion, maintenance
catch-up, cited/empty-index turns, and failed-attempt/rated-retry joins still need
verification against the deployed APIs with valid Bedrock credentials and a
Google ID token (or explicitly configured host loopback local identity).
The offline suite covers these application mechanisms with controlled providers,
but it does not replace the required live acceptance or live observability report.
The existing capture flag does not provide a tested capture-enabled deployment;
default content-free export is verified.

The source deployment reference for Langfuse is its
[v3.160.0 Compose deployment](https://github.com/langfuse/langfuse/blob/v3.160.0/docker-compose.yml).
The session/observation mapping above was verified against that deployed pin,
using the v3 HTTP ingestion contract.
