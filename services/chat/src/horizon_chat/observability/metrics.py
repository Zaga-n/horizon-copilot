"""Bounded metric instruments; dimensions never carry execution, user or document ids."""

from opentelemetry.metrics import Meter

CLIENT_BUCKETS = (
    0.01,
    0.02,
    0.04,
    0.08,
    0.16,
    0.32,
    0.64,
    1.28,
    2.56,
    5.12,
    10.24,
    20.48,
    40.96,
    81.92,
)


class Measurements:
    """Bounded metric dimensions never contain execution, user, document or prompt IDs."""

    def __init__(self, *, meter: Meter) -> None:
        self.duration = meter.create_histogram(
            "app.boundary.duration", unit="s", explicit_bucket_boundaries_advisory=CLIENT_BUCKETS
        )
        self.errors = meter.create_counter("app.boundary.failures")
        self.model_duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            explicit_bucket_boundaries_advisory=CLIENT_BUCKETS,
        )
        self.tokens = meter.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            explicit_bucket_boundaries_advisory=tuple(float(4**i) for i in range(14)),
        )
        self.model_first = meter.create_histogram(
            "gen_ai.client.operation.time_to_first_chunk",
            unit="s",
            explicit_bucket_boundaries_advisory=CLIENT_BUCKETS,
        )
        self.agent_first = meter.create_histogram(
            "app.agent.time_to_first_chunk",
            unit="s",
            explicit_bucket_boundaries_advisory=CLIENT_BUCKETS,
        )
        self.model_calls = meter.create_histogram(
            "gen_ai.invoke_agent.inference_calls", unit="{inference_call}"
        )
        self.tool_calls = meter.create_histogram(
            "gen_ai.invoke_agent.tool_calls", unit="{tool_call}"
        )
        self.results = meter.create_histogram("app.retrieval.result_count", unit="{chunk}")
        self.purged = meter.create_counter("app.retention.purged", unit="{conversation}")
        self.purge_failures = meter.create_counter(
            "app.retention.purge_failures", unit="{conversation}"
        )
        self.recovery_failures = meter.create_counter("app.recovery.failures", unit="{run}")
