"""One SDK owner per process: resource identity, OTLP push export and bounded shutdown."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, Sampler

logger = logging.getLogger(__name__)

DEFAULT_SAMPLER = ParentBased(ALWAYS_ON)
EXPORT_TIMEOUT_SECONDS = 2  # one push attempt; a slow collector must not stall requests
SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceIdentity:
    namespace: str
    name: str
    version: str
    instance_id: str
    environment: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Providers:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider


@asynccontextmanager
async def open_providers(
    *,
    identity: ResourceIdentity,
    endpoint: str | None,
    sampler: Sampler = DEFAULT_SAMPLER,
    shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
) -> AsyncIterator[Providers]:
    """Explicit providers (never global); no endpoint keeps telemetry in-process only."""
    resource = Resource.create(
        {
            "service.namespace": identity.namespace,
            "service.name": identity.name,
            "service.version": identity.version,
            "service.instance.id": identity.instance_id,
            "deployment.environment.name": identity.environment,
        }
    )
    traces = TracerProvider(resource=resource, sampler=sampler)
    readers: list[MetricReader] = []
    if endpoint:
        base = endpoint.rstrip("/")
        traces.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=EXPORT_TIMEOUT_SECONDS)
            )
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=EXPORT_TIMEOUT_SECONDS)
            )
        )
    metrics = MeterProvider(resource=resource, metric_readers=readers)
    try:
        yield Providers(tracer_provider=traces, meter_provider=metrics)
    finally:
        for shutdown in (traces.shutdown, metrics.shutdown):
            try:
                await asyncio.wait_for(asyncio.to_thread(shutdown), shutdown_timeout_seconds)
            except TimeoutError:
                # Best-effort flush: losing the last batch beats blocking process exit.
                logger.warning("telemetry_shutdown_timeout")
