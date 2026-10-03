"""Run roots and boundary spans; run roots remain independent of inbound traces."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Literal
from uuid import UUID

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Span, Tracer

from horizon_chat.domain.runs import FailureCategory, Run, RunIdentity
from horizon_chat.observability.metrics import Measurements
from horizon_observability import boundary_span, outcome_of

# Re-exported: other observability modules mark spans through this one, never the library.
from horizon_observability import mark_error as mark_error

logger = logging.getLogger(__name__)

type Boundary = Literal["agent", "model", "tool", "retrieval", "embedding", "maintenance", "http"]


class TurnObservation:
    """One attempt's root span, outcome logs and turn metrics, from reservation to end.

    Mutated only by its own methods. The span is active only inside `active()`, never across
    a generator yield, and actions report outcomes here without touching the span.
    """

    def __init__(self, *, span: Span, identity: RunIdentity, measurements: Measurements) -> None:
        self.identity = identity
        self._span = span
        self._measurements = measurements
        self._started = perf_counter()
        self._first_answer_recorded = False
        self._log_fields: dict[str, str | int] = {}

    @contextmanager
    def active(self) -> Iterator[None]:
        with trace.use_span(
            self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        ):
            yield

    def admitted(self, run: Run) -> None:
        self._span.update_name("gen_ai.invoke_agent")
        self._span.set_attributes(
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "Horizon",
                "app.conversation.id": str(run.conversation_id),
                "app.thread.id": str(run.conversation_id),
                "app.turn.id": str(run.turn_id),
                "app.run.id": str(run.id),
                "app.user_message.id": str(run.user_message_id),
                "app.assistant_message.id": str(run.assistant_message_id),
                "app.attempt.number": run.attempt_number,
                "app.agent.version": run.agent_version,
                "app.prompt.version": run.prompt_version,
                "app.retrieval.version": run.retrieval_version,
            }
        )
        self._log_fields = {
            "conversation_id": str(run.conversation_id),
            "turn_id": str(run.turn_id),
            "run_id": str(run.id),
            "assistant_message_id": str(run.assistant_message_id),
            "attempt_number": run.attempt_number,
        }

    def abandoned(self, exc: BaseException) -> None:
        """Admission failed, so no attempt exists; the root ends as an error."""
        mark_error(self._span, exc)
        self._span.end()

    def discard(self) -> None:
        """End the root without an attempt: the request replayed an earlier one."""
        self._span.end()

    def answer_started(self) -> None:
        """Record time to the first visible answer text, once per attempt, on metric and root."""
        if not self._first_answer_recorded:
            self._first_answer_recorded = True
            elapsed = perf_counter() - self._started
            self._measurements.agent_first.record(elapsed)
            self._span.set_attribute("app.agent.time_to_first_chunk", elapsed)

    def completed(self) -> None:
        logger.info("turn_completed", extra=self._log_fields)

    def cancelled(self, exc: BaseException) -> None:
        mark_error(self._span, exc)
        logger.info("turn_cancelled", extra=self._log_fields)

    def failed(
        self, exc: Exception, *, category: FailureCategory, persistence_pending: bool
    ) -> None:
        """The turn's one terminal failure record: it ends the operation, so it is an error."""
        mark_error(self._span, exc)
        logger.error(
            "turn_failed",
            extra={
                **self._log_fields,
                "failure_category": category.value,
                "persistence_pending": persistence_pending,
            },
            exc_info=exc,
        )

    def end(self, *, completed: bool) -> None:
        self._measurements.duration.record(
            perf_counter() - self._started,
            {"app.boundary": "agent", "outcome": "ok" if completed else "error"},
        )
        self._span.end()


@dataclass(frozen=True, slots=True, kw_only=True)
class Telemetry:
    """Explicit SDK injection avoids competing global providers and supports unsampled tests."""

    tracer: Tracer
    measurements: Measurements

    def reserve(
        self,
        *,
        run_id: UUID,
        assistant_message_id: UUID,
        agent_version: str,
        prompt_version: str,
        retrieval_version: str,
    ) -> TurnObservation:
        """Start an unlinked root span for ids the action minted; tracing mints none itself."""
        incoming = trace.get_current_span().get_span_context()
        span = self.tracer.start_span(
            "chat.admission", context=Context(), links=[Link(incoming)] if incoming.is_valid else []
        )
        context = span.get_span_context()
        return TurnObservation(
            span=span,
            measurements=self.measurements,
            identity=RunIdentity(
                id=run_id,
                assistant_message_id=assistant_message_id,
                trace_id=f"{context.trace_id:032x}",
                root_span_id=f"{context.span_id:016x}",
                agent_version=agent_version,
                prompt_version=prompt_version,
                retrieval_version=retrieval_version,
            ),
        )

    @contextmanager
    def work(self, boundary: Boundary) -> Iterator[Span]:
        started = perf_counter()
        outcome = "ok"
        try:
            with boundary_span(self.tracer, f"app.{boundary}") as span:
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
