"""Telemetry for tests that exercise behavior without observing spans or metrics."""

from opentelemetry.metrics import NoOpMeter
from opentelemetry.trace import NoOpTracer

from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry


def quiet_telemetry() -> Telemetry:
    return Telemetry(tracer=NoOpTracer(), measurements=Measurements(meter=NoOpMeter("test")))
