"""Boundary spans, error marking, outcome naming and W3C trace-context carriers."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Literal

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

type FailureOutcome = Literal["cancelled", "error"]

CARRIER_KEYS = frozenset({"traceparent", "tracestate"})
MAX_CARRIER_VALUE_CHARS = 512  # stored carriers are untrusted; W3C values are far shorter


def mark_error(span: Span, exc: BaseException) -> None:
    """Error status and the exception class only; messages may carry content."""
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", type(exc).__name__)


def outcome_of(exc: BaseException) -> FailureOutcome:
    """Cancellation is a shutdown or disconnect, not a failure of the work."""
    return "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"


def inject_carrier() -> dict[str, str]:
    """The current trace context, for storage beside durable work."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier


def extract_link(carrier: Mapping[str, str]) -> Link | None:
    """A link to the producer recorded in a stored carrier; None when it holds no valid context."""
    allowed = {
        key: value
        for key, value in carrier.items()
        if key in CARRIER_KEYS and len(value) <= MAX_CARRIER_VALUE_CHARS
    }
    parent = trace.get_current_span(
        TraceContextTextMapPropagator().extract(allowed, context=Context())
    ).get_span_context()
    return Link(parent) if parent.is_valid else None


@contextmanager
def boundary_span(
    tracer: Tracer, name: str, *, carrier: Mapping[str, str] | None = None
) -> Iterator[Span]:
    """The current span for one unit of work at a service boundary.

    A stored `carrier` starts a new root linked to its producer, never a child of it. A failure
    marks the span with its class only; cancellation is not marked. The exception propagates,
    so callers record their own metrics from `outcome_of`.
    """
    link = extract_link(carrier) if carrier is not None else None
    with tracer.start_as_current_span(
        name,
        context=Context() if carrier is not None else None,
        links=[link] if link is not None else [],
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            if outcome_of(exc) == "error":
                mark_error(span, exc)
            raise
