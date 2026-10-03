"""One explicit SDK/OTLP owner with linked durable roots and bounded metric attributes."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from time import perf_counter
from typing import Literal
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from horizon_observability import (
    ResourceIdentity,
    boundary_span,
    inject_carrier,
    open_providers,
    outcome_of,
)

type Boundary = Literal[
    "upload",
    "job",
    "extraction",
    "embedding",
    "publication",
    "cleanup",
    "reconciliation",
    "status",
    "retry",
    "delete",
]


class Measurements:
    """Only bounded kind/state/error vocabularies are metric dimensions."""

    def __init__(self, *, meter: Meter) -> None:
        self.duration = meter.create_histogram("app.ingestion.duration", unit="s")
        self.errors = meter.create_counter("app.ingestion.failures")
        self.jobs = meter.create_counter("app.ingestion.jobs")
        self.chunks = meter.create_counter("app.ingestion.chunks.completed")
        self.vendor_calls = meter.create_counter("app.ingestion.vendor.calls")
        self.retries = meter.create_counter("app.ingestion.retries")
        self.queue_age = meter.create_histogram("app.ingestion.queue.age", unit="s")
        self.tokens = meter.create_histogram("gen_ai.client.token.usage", unit="{token}")
        self.orphans = meter.create_counter("app.ingestion.orphans.removed")


def mark_category_error(category: str) -> None:
    span = trace.get_current_span()
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", category)


@dataclass(frozen=True, slots=True, kw_only=True)
class Telemetry:
    """Own boundary lifecycle without storing content or changing application outcomes."""

    tracer: Tracer
    measurements: Measurements

    @contextmanager
    def work(self, boundary: Boundary, *, carrier: dict[str, str] | None = None) -> Iterator[Span]:
        started = perf_counter()
        outcome = "ok"
        try:
            with boundary_span(self.tracer, f"app.{boundary}", carrier=carrier) as span:
                yield span
        except BaseException as exc:
            outcome = outcome_of(exc)
            if outcome == "error":
                self.measurements.errors.add(1, {"app.boundary": boundary})
            raise
        finally:
            self.measurements.duration.record(
                perf_counter() - started, {"app.boundary": boundary, "outcome": outcome}
            )

    def chunk_completed(self) -> None:
        self.measurements.chunks.add(1)

    def chunk_retried(self, *, category: str) -> None:
        self.measurements.retries.add(1, {"scope": "chunk", "category": category})

    def carrier(self) -> dict[str, str]:
        return inject_carrier()


@asynccontextmanager
async def open_telemetry(
    *, environment: str, endpoint: str | None, instance_id: str | None
) -> AsyncIterator[Telemetry]:
    identity = ResourceIdentity(
        namespace="horizon",
        name="horizon-ingestion",
        version=version("horizon-ingestion"),
        instance_id=instance_id or str(uuid4()),
        environment=environment,
    )
    async with open_providers(identity=identity, endpoint=endpoint) as providers:
        yield Telemetry(
            tracer=providers.tracer_provider.get_tracer("horizon_ingestion"),
            measurements=Measurements(
                meter=providers.meter_provider.get_meter("horizon_ingestion")
            ),
        )
