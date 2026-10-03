"""Linked roots, privacy and Titan SDK attempts at the real LangChain boundary."""

import asyncio
import json
import logging
from io import BytesIO

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from langchain_aws import BedrockEmbeddings
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from horizon_ingestion.genai.embeddings import TitanEmbeddings, build_document_embeddings
from horizon_ingestion.observability.logging import JsonFormatter
from horizon_ingestion.observability.tracing import Measurements, Telemetry
from horizon_ingestion.ports.indexing import EmbeddingUnavailableError


async def test_real_langchain_titan_call_records_usage_without_content() -> None:
    spans = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    metrics = MeterProvider(metric_readers=[reader])
    telemetry = Telemetry(
        tracer=traces.get_tracer("test"), measurements=Measurements(meter=metrics.get_meter("test"))
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name="eu-west-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    model_id = "amazon.titan-embed-text-v2:0"
    try:
        body = json.dumps({"embedding": [1.0] + [0.0] * 1023, "inputTextTokenCount": 7}).encode()
        with Stubber(client) as stub:
            stub.add_response(
                "invoke_model",
                {
                    "body": StreamingBody(BytesIO(body), len(body)),
                    "contentType": "application/json",
                },
                {
                    "body": json.dumps({"inputText": "CONTENT_CANARY", "dimensions": 1024}),
                    "modelId": model_id,
                    "accept": "application/json",
                    "contentType": "application/json",
                },
            )
            embeddings = build_document_embeddings(
                client=client, model_id=model_id, dimensions=1024, telemetry=telemetry
            )
            with telemetry.work("upload") as upload:
                carrier = telemetry.carrier()
                producer = upload.get_span_context()
            with telemetry.work("job", carrier=carrier):
                vector = await embeddings.embed(text="CONTENT_CANARY")
                record = logging.LogRecord(
                    "horizon_ingestion.worker",
                    logging.INFO,
                    __file__,
                    1,
                    "ingestion_job_ready",
                    (),
                    None,
                )
                record.job_id = "opaque-job"
                record.content = "CONTENT_CANARY"
                rendered = JsonFormatter(full_exception_trace=True).format(record)
                assert json.loads(rendered)["trace_id"]
            assert len(vector) == 1024
            stub.assert_no_pending_responses()
        job = next(span for span in spans.get_finished_spans() if span.name == "app.job")
        embedding = next(
            span for span in spans.get_finished_spans() if span.name == "app.embedding"
        )
        assert job.parent is None
        assert job.links[0].context.trace_id == producer.trace_id
        assert job.links[0].context.span_id == producer.span_id
        assert embedding.parent == job.context
        assert embedding.attributes is not None
        assert embedding.attributes["gen_ai.usage.input_tokens"] == 7
        assert "CONTENT_CANARY" not in rendered
        assert "CONTENT_CANARY" not in str(
            [dict(span.attributes or {}) for span in spans.get_finished_spans()]
        )
        collected = reader.get_metrics_data()
        assert collected is not None
        dimensions = [
            dict(point.attributes or {})
            for resource in collected.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            for point in metric.data.data_points
        ]
        assert all(
            not {"job_id", "document_id", "sha256"} & dimension.keys() for dimension in dimensions
        )
    finally:
        client.close()
        traces.shutdown()
        metrics.shutdown()


async def test_provider_failure_is_error_without_exception_payload() -> None:
    spans = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(spans))
    metrics = MeterProvider()
    telemetry = Telemetry(
        tracer=traces.get_tracer("test"), measurements=Measurements(meter=metrics.get_meter("test"))
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name="eu-west-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    try:
        with Stubber(client) as stub:
            stub.add_client_error(
                "invoke_model",
                service_error_code="AccessDeniedException",
                service_message="SECRET_CANARY",
                http_status_code=403,
            )
            model = BedrockEmbeddings(
                client=client, model_id="amazon.titan-embed-text-v2:0", dimensions=1024
            )
            # Denied credentials fail every request alike, so the job waits instead of failing.
            with pytest.raises(EmbeddingUnavailableError):
                await TitanEmbeddings(
                    model=model,
                    model_id="amazon.titan-embed-text-v2:0",
                    dimensions=1024,
                    telemetry=telemetry,
                ).embed(text="CONTENT_CANARY")
        span = spans.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.events == ()
        assert "SECRET_CANARY" not in str(span.attributes)
    finally:
        client.close()
        traces.shutdown()
        metrics.shutdown()


async def test_cancelled_work_is_reported_as_cancelled_not_error() -> None:
    spans = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    metrics = MeterProvider(metric_readers=[reader])
    telemetry = Telemetry(
        tracer=traces.get_tracer("test"), measurements=Measurements(meter=metrics.get_meter("test"))
    )

    async def shutdown_mid_job() -> None:
        with telemetry.work("job"):
            await asyncio.sleep(10)

    task = asyncio.create_task(shutdown_mid_job())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    span = spans.get_finished_spans()[0]
    assert span.status.status_code != StatusCode.ERROR
    collected = reader.get_metrics_data()
    assert collected is not None
    outcomes = {
        dict(point.attributes or {}).get("outcome")
        for resource in collected.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "app.ingestion.duration"
        for point in metric.data.data_points
    }
    assert outcomes == {"cancelled"}
    traces.shutdown()
    metrics.shutdown()
