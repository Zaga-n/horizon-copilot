# horizon-observability

OpenTelemetry provider lifecycle, the safe JSON log formatter, span error marking and
trace-context carriers shared by `horizon-chat` and `horizon-ingestion`.

**Kind:** observability. **Importers:** each service's `observability/` and `bootstrap/` packages,
enforced by import-linter contracts in the workspace `pyproject.toml`.

## Why this is shared

Both services carried identical provider setup and log formatting that had already drifted: chat
sampled with `ALWAYS_ON` while ingestion used the parent-based default, and only ingestion
reported cancellation as `cancelled`. The two copies were meant to be the same.

**Stays in each service:** span vocabulary (`Boundary`), `Measurements`, `Telemetry`, event and
field allowlists, GenAI callbacks and HTTP middleware.

## Contract

- Explicit typed inputs; the library reads no environment and configures no logging until
  bootstrap calls `install_json_logging`.
- Shutdown is bounded and runs off the event loop.
- Logs carry only allowlisted fields; message text of other loggers and exception messages are
  never emitted.
