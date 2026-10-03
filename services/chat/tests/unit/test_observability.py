"""Attribution survives sampling/export failures without recording private content."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from horizon_chat.config.settings import Settings
from horizon_chat.observability.genai import ModelTelemetry
from horizon_chat.observability.logging import JsonFormatter
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry, TurnObservation

PRIVATE = "PRIVATE prompt chunk key credential"


def reserve(telemetry: Telemetry) -> TurnObservation:
    return telemetry.reserve(
        run_id=uuid4(),
        assistant_message_id=uuid4(),
        agent_version="test",
        prompt_version="v1",
        retrieval_version="test",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    telemetry: Telemetry
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader


@pytest.fixture
def observation() -> Iterator[Observation]:
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    try:
        yield Observation(
            telemetry=Telemetry(
                tracer=traces.get_tracer("test"),
                measurements=Measurements(meter=meters.get_meter("test")),
            ),
            exporter=exporter,
            reader=reader,
        )
    finally:
        traces.shutdown()
        meters.shutdown()


async def test_physical_attempt_spans_usage_and_first_chunk_are_content_free(
    observation: Observation,
) -> None:
    telemetry = observation.telemetry
    root = reserve(telemetry)
    callback = ModelTelemetry(telemetry=telemetry, model_id="terra", prompt_version="v1")
    first, second = uuid4(), uuid4()
    with root.active(), telemetry.work("tool"), telemetry.work("retrieval"):
        await callback.on_chat_model_start({}, [[HumanMessage(content=PRIVATE)]], run_id=first)
        await callback.on_llm_error(RuntimeError(PRIVATE), run_id=first)
        await callback.on_chat_model_start({}, [[HumanMessage(content=PRIVATE)]], run_id=second)
        for _ in range(100):
            await callback.on_llm_new_token(PRIVATE, run_id=second)
        await callback.on_llm_end(
            LLMResult(
                generations=[
                    [
                        ChatGeneration(
                            message=AIMessage(
                                content=PRIVATE,
                                usage_metadata={
                                    "input_tokens": 17,
                                    "output_tokens": 23,
                                    "total_tokens": 40,
                                },
                            )
                        )
                    ]
                ]
            ),
            run_id=second,
        )
    root.discard()
    spans = observation.exporter.get_finished_spans()
    assert len(spans) == 5
    models = [span for span in spans if span.name == "gen_ai.chat"]
    assert len(models) == 2 and models[0].status.status_code.name == "ERROR"
    assert models[1].attributes and models[1].attributes["gen_ai.usage.input_tokens"] == 17
    assert len({span.context.trace_id for span in spans if span.context}) == 1
    assert PRIVATE not in str([(span.attributes, span.events) for span in spans])
    data = observation.reader.get_metrics_data()
    assert data is not None
    metrics = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    points = metrics["gen_ai.client.operation.time_to_first_chunk"].data.data_points
    assert sum(point.count for point in points if isinstance(point, HistogramDataPoint)) == 1
    assert PRIVATE not in str(data)
    assert all(
        "run" not in key and "user" not in key
        for metric in metrics.values()
        for point in metric.data.data_points
        for key in (point.attributes or {})
    )


@pytest.mark.parametrize("full_trace", [False, True])
def test_json_logs_redact_messages_exceptions_and_unknown_fields(full_trace: bool) -> None:
    formatter = JsonFormatter(full_exception_trace=full_trace)
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        record = logging.getLogger("horizon_chat.test").makeRecord(
            "horizon_chat.test",
            logging.ERROR,
            __file__,
            1,
            "turn_failed",
            (),
            __import__("sys").exc_info(),
            extra={"run_id": "stable", "prompt": PRIVATE},
        )
    payload = json.loads(formatter.format(record))
    assert payload["run_id"] == "stable" and payload["error.type"] == "RuntimeError"
    assert PRIVATE not in json.dumps(payload)
    third_party = logging.makeLogRecord({"name": "sdk", "msg": PRIVATE, "args": ()})
    assert PRIVATE not in formatter.format(third_party)
    assert ("exception.frames" in payload) == full_trace


class BrokenExporter(SpanExporter):
    def export(self, spans: object) -> SpanExportResult:
        raise RuntimeError(PRIVATE)


@pytest.mark.parametrize("sampled", [True, False])
def test_fresh_root_ids_survive_unsampled_and_export_failure(sampled: bool) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    provider.add_span_processor(SimpleSpanProcessor(BrokenExporter()))
    meters = MeterProvider()
    telemetry = Telemetry(
        tracer=provider.get_tracer("test"),
        measurements=Measurements(meter=meters.get_meter("test")),
    )
    incoming = SpanContext(trace_id=123, span_id=456, is_remote=True, trace_flags=TraceFlags(1))
    with trace.use_span(NonRecordingSpan(incoming)):
        first = reserve(telemetry)
        second = reserve(telemetry)
    assert first.identity.trace_id != second.identity.trace_id
    assert int(first.identity.trace_id, 16) not in (0, 123)
    first.discard()
    second.discard()
    provider.shutdown()
    meters.shutdown()


async def test_http_latency_and_error_metrics_exclude_request_paths_and_identity(
    observation: Observation,
    chat_settings: Settings,
) -> None:
    from types import SimpleNamespace

    import httpx

    from horizon_chat.bootstrap.app import create_app

    class Unready:
        async def check(self) -> bool:
            return False

    app = create_app(settings=chat_settings)
    app.state.runtime = SimpleNamespace(
        telemetry=observation.telemetry, readiness=Unready(), readiness_timeout_seconds=1
    )
    app.state.liveness = SimpleNamespace(loops_alive=lambda: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health", headers={"Authorization": PRIVATE})).status_code == 200
        assert (await client.get("/ready")).status_code == 503
    data = observation.reader.get_metrics_data()
    assert data is not None
    assert PRIVATE not in str(data) and "/health" not in str(data)
    metrics = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert (
        sum(
            point.count
            for point in metrics["app.boundary.duration"].data.data_points
            if isinstance(point, HistogramDataPoint)
        )
        == 2
    )
    assert (
        sum(
            point.value
            for point in metrics["app.boundary.failures"].data.data_points
            if isinstance(point, NumberDataPoint)
        )
        == 1
    )
