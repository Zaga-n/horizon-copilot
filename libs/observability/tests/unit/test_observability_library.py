"""Provider lifecycle, safe JSON logs, error marking and carriers over in-memory exporters."""

import asyncio
import json
import logging
import sys
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased
from opentelemetry.trace import StatusCode

from horizon_observability import (
    JsonLogFormatter,
    ResourceIdentity,
    boundary_span,
    extract_link,
    inject_carrier,
    install_json_logging,
    mark_error,
    open_providers,
    outcome_of,
)

PRIVATE = "PRIVATE-CONTENT"
IDENTITY = ResourceIdentity(
    namespace="horizon",
    name="horizon-test",
    version="1.0",
    instance_id="instance-1",
    environment="local",
)


def formatter(*, full_exception_trace: bool = False) -> JsonLogFormatter:
    return JsonLogFormatter(
        service_name="horizon-test",
        logger_prefix="horizon_test",
        events=frozenset({"job_failed"}),
        fields=frozenset({"job_id"}),
        full_exception_trace=full_exception_trace,
    )


def failed_record() -> logging.LogRecord:
    try:
        raise RuntimeError(PRIVATE)
    except RuntimeError:
        return logging.getLogger("horizon_test.worker").makeRecord(
            "horizon_test.worker",
            logging.ERROR,
            __file__,
            1,
            "job_failed",
            (),
            sys.exc_info(),
            extra={"job_id": "opaque", "prompt": PRIVATE},
        )


@pytest.mark.parametrize("full_trace", [False, True])
def test_logs_keep_allowlisted_fields_and_drop_content(full_trace: bool) -> None:
    payload = json.loads(formatter(full_exception_trace=full_trace).format(failed_record()))
    assert payload["event"] == "job_failed"
    assert payload["service.name"] == "horizon-test"
    assert payload["job_id"] == "opaque"
    assert payload["dropped_fields"] == ["prompt"]
    assert payload["error.type"] == "RuntimeError"
    assert ("exception.frames" in payload) == full_trace
    assert PRIVATE not in json.dumps(payload)


def test_other_loggers_and_unknown_events_never_emit_their_message() -> None:
    record = logging.makeLogRecord({"name": "sdk", "msg": PRIVATE, "args": ()})
    payload = json.loads(formatter().format(record))
    assert payload["event"] == "library_log"
    assert PRIVATE not in json.dumps(payload)


class DatabaseConnectionError(Exception):
    """A library error whose text is private but SQLSTATE is safe to retain."""

    sqlstate = "28P01"


@pytest.mark.parametrize("full_trace", [False, True])
def test_pool_warning_explains_connection_failure_without_credentials(full_trace: bool) -> None:
    record = logging.makeLogRecord(
        {
            "name": "psycopg.pool",
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": "error connecting in %r: %s",
            "args": (PRIVATE, DatabaseConnectionError(f"password={PRIVATE}")),
        }
    )
    payload = json.loads(formatter(full_exception_trace=full_trace).format(record))
    assert payload["event"] == "database_connection_failed"
    assert payload["level"] == "warning"
    assert payload["error.type"] == "DatabaseConnectionError"
    assert payload["db.sqlstate"] == "28P01"
    assert PRIVATE not in json.dumps(payload)


def test_pool_warning_distinguishes_returned_transaction_from_connection_failure() -> None:
    record = logging.makeLogRecord(
        {
            "name": "psycopg.pool",
            "msg": "rolling back returned connection: %s",
            "args": (f"connection password={PRIVATE}",),
        }
    )
    payload = json.loads(formatter().format(record))
    assert payload["event"] == "database_returned_connection_rolled_back"
    assert "error.type" not in payload
    assert PRIVATE not in json.dumps(payload)


@pytest.mark.parametrize(
    ("logger_name", "message"),
    [("sdk", "error connecting in %r: %s"), ("psycopg.pool", PRIVATE)],
)
def test_unknown_library_warning_cannot_bypass_safe_event_allowlist(
    logger_name: str, message: str
) -> None:
    record = logging.makeLogRecord(
        {"name": logger_name, "msg": message, "args": (PRIVATE, DatabaseConnectionError(PRIVATE))}
    )
    payload = json.loads(formatter().format(record))
    assert payload["event"] == "library_log"
    assert "error.type" not in payload
    assert PRIVATE not in json.dumps(payload)


def test_pool_error_rejects_private_or_malformed_sqlstate() -> None:
    error = DatabaseConnectionError(PRIVATE)
    error.sqlstate = PRIVATE
    record = logging.makeLogRecord(
        {"name": "psycopg.pool", "msg": "error resetting connection: %s", "args": (error,)}
    )
    payload = json.loads(formatter().format(record))
    assert payload["event"] == "database_connection_reset_failed"
    assert "db.sqlstate" not in payload
    assert PRIVATE not in json.dumps(payload)


def test_install_replaces_root_handlers(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        install_json_logging(formatter=formatter(), level="INFO")
        logging.getLogger("horizon_test.worker").info("job_failed", extra={"job_id": "x"})
        assert json.loads(capsys.readouterr().out)["job_id"] == "x"
    finally:
        root.handlers[:] = saved
        root.setLevel(level)


def test_cancellation_is_not_an_error() -> None:
    assert outcome_of(asyncio.CancelledError()) == "cancelled"
    assert outcome_of(RuntimeError(PRIVATE)) == "error"


def test_error_marking_records_only_the_class() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("work") as span:
        mark_error(span, RuntimeError(PRIVATE))
    finished = exporter.get_finished_spans()[0]
    assert finished.status.status_code == StatusCode.ERROR
    assert finished.attributes == {"error.type": "RuntimeError"}
    provider.shutdown()


def test_carriers_round_trip_and_reject_oversized_or_foreign_keys() -> None:
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("producer") as producer:
        carrier = inject_carrier()
    link = extract_link(carrier)
    assert link is not None
    assert link.context.trace_id == producer.get_span_context().trace_id
    assert extract_link({"traceparent": carrier["traceparent"] + "x" * 600}) is None
    assert extract_link({"baggage": "secret=1"}) is None
    assert extract_link({}) is None
    provider.shutdown()


async def test_providers_are_parent_based_and_shut_down_within_the_bound() -> None:
    async with open_providers(identity=IDENTITY, endpoint=None) as providers:
        assert isinstance(providers.tracer_provider.sampler, ParentBased)
        resource = providers.tracer_provider.resource.attributes
        assert resource["service.name"] == "horizon-test"
        assert resource["service.instance.id"] == "instance-1"
        tracer = providers.tracer_provider.get_tracer("test")
        with tracer.start_as_current_span("root"):
            assert trace.get_current_span().get_span_context().trace_flags.sampled


async def test_a_hung_exporter_cannot_block_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang() -> None:

        time.sleep(2)

    async with open_providers(
        identity=IDENTITY, endpoint=None, shutdown_timeout_seconds=0.05
    ) as providers:
        monkeypatch.setattr(providers.tracer_provider, "shutdown", hang)
        started = asyncio.get_running_loop().time()
    assert asyncio.get_running_loop().time() - started < 0.4


def recording_tracer() -> tuple[trace.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_a_failed_boundary_is_marked_with_its_class_and_reraised() -> None:
    tracer, exporter = recording_tracer()
    with pytest.raises(RuntimeError), boundary_span(tracer, "app.job"):
        raise RuntimeError(PRIVATE)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes is not None and span.attributes["error.type"] == "RuntimeError"
    assert PRIVATE not in str((span.attributes, span.events))


def test_a_cancelled_boundary_is_not_an_error() -> None:
    tracer, exporter = recording_tracer()
    with pytest.raises(asyncio.CancelledError), boundary_span(tracer, "app.job"):
        raise asyncio.CancelledError
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.UNSET


def test_a_stored_carrier_starts_a_linked_root_not_a_child() -> None:
    tracer, exporter = recording_tracer()
    with tracer.start_as_current_span("app.upload"):
        carrier = inject_carrier()
        with boundary_span(tracer, "app.job", carrier=carrier):
            pass
    job, upload = exporter.get_finished_spans()
    assert job.parent is None
    assert upload.context is not None
    assert [link.context.span_id for link in job.links] == [upload.context.span_id]
