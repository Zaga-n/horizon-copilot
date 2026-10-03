"""Record optional Titan input-token usage without capturing text."""

from collections.abc import Callable

from opentelemetry import trace

from horizon_ingestion.observability.tracing import Telemetry


def embedding_usage(telemetry: Telemetry) -> Callable[[int], None]:
    """A usage callback that sets the current span attribute and the token histogram."""

    def record(count: int) -> None:
        trace.get_current_span().set_attribute("gen_ai.usage.input_tokens", count)
        telemetry.measurements.tokens.record(
            count, {"gen_ai.token.type": "input", "gen_ai.operation.name": "embeddings"}
        )

    return record
