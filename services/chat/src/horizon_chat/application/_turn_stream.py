"""Stream one admitted attempt: pin its checkpoint, persist each visible prefix, settle once.

Private to the turn actions in `submit_turn.py`; no entry point calls it directly.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from horizon_chat.domain.agent import (
    AgentEvent,
    AnswerDelta,
    AnswerSources,
)
from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.recovery import FailureIntent, RecoveryBuffer
from horizon_chat.domain.runs import (
    Admission,
    AttemptStatus,
    CheckpointSource,
    CheckpointStart,
    ConversationBusyError,
    FailureCategory,
    Run,
    TurnPolicy,
    checkpoint_start,
)
from horizon_chat.domain.sources import Source
from horizon_chat.domain.streaming import EventType, StreamEvent
from horizon_chat.observability.tracing import TurnObservation
from horizon_chat.ports.agent import (
    AgentBudgetError,
    AgentIndexIntegrityError,
    AgentOutputError,
    AgentProtocolError,
    AgentRejectedError,
    AgentUnavailableError,
    HorizonAgent,
)
from horizon_chat.ports.checkpoints import CheckpointStore
from horizon_chat.ports.conversations import (
    ConversationStoreIntegrityError,
    ConversationStoreUnavailableError,
)
from horizon_chat.ports.errors import DependencyUnavailableError
from horizon_chat.ports.runs import RunLedger

FAILED_TEXT = "The answer could not be completed. Please check the saved turn before retrying."

# Ordered: the first matching type names the outcome.
FAILURE_CATEGORIES: tuple[tuple[type[Exception], FailureCategory], ...] = (
    (AgentBudgetError, FailureCategory.BUDGET_EXHAUSTED),
    (AgentProtocolError, FailureCategory.PROVIDER_PROTOCOL),
    (AgentOutputError, FailureCategory.INVALID_CITATIONS),
    (AgentIndexIntegrityError, FailureCategory.INDEX_INTEGRITY),
    (AgentRejectedError, FailureCategory.PROVIDER_REJECTED),
    (AgentUnavailableError, FailureCategory.SERVICE_UNAVAILABLE),
    (DependencyUnavailableError, FailureCategory.SERVICE_UNAVAILABLE),
    # Admission already succeeded, so a busy conversation mid-attempt means the lease expired
    # and another actor took over or failed the run.
    (ConversationBusyError, FailureCategory.INTERRUPTED),
)


def failure_category(exc: Exception) -> FailureCategory:
    return next(
        (category for error, category in FAILURE_CATEGORIES if isinstance(exc, error)),
        FailureCategory.INTERNAL,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _FailureRecord:
    persistence_pending: bool  # a recovery intent stays queued for the reconciliation loop
    retry_available: bool


@dataclass(slots=True, kw_only=True)
class _EventSequence:
    """Mutated by `event` only: numbers the attempt's events and remembers the last type."""

    run: Run
    sequence: int = 0
    last: EventType = "started"

    def event(self, **values: object) -> StreamEvent:
        self.sequence += 1
        result = StreamEvent.model_validate(
            {
                "conversation_id": self.run.conversation_id,
                "turn_id": self.run.turn_id,
                "run_id": self.run.id,
                "assistant_message_id": self.run.assistant_message_id,
                "trace_id": self.run.trace_id,
                "sequence": self.sequence,
                "event_id": f"{self.run.id}:{self.sequence}",
                "attempt_number": self.run.attempt_number,
                "event": values.pop("event"),
                "data": values,
            }
        )
        self.last = result.event
        return result


async def stream_attempt(
    *,
    runs: RunLedger,
    checkpoints: CheckpointStore,
    agent: HorizonAgent,
    recovery: RecoveryBuffer,
    policy: TurnPolicy,
    observation: TurnObservation,
    subject: str,
    admission: Admission,
) -> AsyncIterator[StreamEvent]:
    run = admission.run
    events = _EventSequence(run=run)
    content = ""
    sources: tuple[Source, ...] = ()
    iterator: AsyncGenerator[AgentEvent] | None = None
    try:
        yield events.event(event="started")
        with observation.active():
            start = await prepare_attempt(
                runs=runs, checkpoints=checkpoints, subject=subject, run_id=run.id
            )
        iterator = agent.answer(subject=subject, admission=admission, start=start)
        while True:
            try:
                with observation.active():
                    item = await anext(iterator)
            except StopAsyncIteration:
                break
            if isinstance(item, AnswerDelta):
                content += item.text
                with observation.active():
                    await runs.save_partial(subject=subject, run_id=run.id, content=content)
                observation.answer_started()
                yield events.event(event="delta", text=item.text)
            elif isinstance(item, AnswerSources):
                sources = item.sources
            else:
                yield events.event(event="progress", phase=item.phase, message=item.message)
        with observation.active():
            await runs.finish(
                subject=subject,
                run_id=run.id,
                status=AttemptStatus.COMPLETED,
                content=content,
                sources=sources,
                failure_category=None,
            )
        observation.completed()
        yield events.event(event="sources", sources=sources)
        yield events.event(event="completed")
    except (asyncio.CancelledError, GeneratorExit) as exc:
        with observation.active():
            observation.cancelled(exc)
            await _cancel(
                runs=runs, recovery=recovery, policy=policy, subject=subject, admission=admission
            )
        raise
    except Exception as exc:  # noqa: BLE001 Turn boundary: every failure ends the attempt with a named category, and TurnObservation logs it once with its traceback.
        yield await _failed(
            exc,
            runs=runs,
            recovery=recovery,
            policy=policy,
            observation=observation,
            subject=subject,
            admission=admission,
            events=events,
        )
    finally:
        try:
            if iterator is not None:
                with observation.active():
                    await iterator.aclose()
        finally:
            observation.end(completed=events.last == "completed")


async def _failed(
    exc: Exception,
    *,
    runs: RunLedger,
    recovery: RecoveryBuffer,
    policy: TurnPolicy,
    observation: TurnObservation,
    subject: str,
    admission: Admission,
    events: _EventSequence,
) -> StreamEvent:
    """Settle a failed attempt and return its terminal event."""
    category = failure_category(exc)
    with observation.active():
        record = await _fail(
            runs=runs,
            recovery=recovery,
            policy=policy,
            subject=subject,
            admission=admission,
            category=category,
        )
        observation.failed(exc, category=category, persistence_pending=record.persistence_pending)
    return events.event(
        event="failed",
        failure_category=category,
        persistence_pending=record.persistence_pending,
        retry_available=record.retry_available,
        text=FAILED_TEXT,
    )


async def prepare_attempt(
    *, runs: RunLedger, checkpoints: CheckpointStore, subject: str, run_id: UUID
) -> CheckpointStart:
    """Pin the attempt to its pre-input checkpoint before the graph runs."""
    plan = await runs.checkpoint_plan(subject=subject, run_id=run_id)
    source = checkpoint_start(plan)
    checkpoint_id = plan.base_checkpoint_id
    if source == CheckpointSource.CURRENT:
        checkpoint_id = await checkpoints.current_id(conversation_id=plan.conversation_id)
    elif source == CheckpointSource.EMPTY:
        await checkpoints.delete_thread(conversation_id=plan.conversation_id)
    if source != CheckpointSource.RECORDED:
        await runs.record_checkpoint(subject=subject, run_id=run_id, checkpoint_id=checkpoint_id)
    if checkpoint_id is not None:
        await checkpoints.require_checkpoint(
            conversation_id=plan.conversation_id, checkpoint_id=checkpoint_id
        )
    return CheckpointStart(conversation_id=plan.conversation_id, checkpoint_id=checkpoint_id)


async def _fail(
    *,
    runs: RunLedger,
    recovery: RecoveryBuffer,
    policy: TurnPolicy,
    subject: str,
    admission: Admission,
    category: FailureCategory,
) -> _FailureRecord:
    """Persist the failed outcome, queuing a recovery intent while the store is unavailable."""
    intent = FailureIntent(
        subject=subject,
        conversation_id=admission.run.conversation_id,
        run_id=admission.run.id,
        category=category,
    )
    # Queue before writes so cancellation of cleanup still leaves a reconciliation intent.
    recovery.add(intent)
    try:
        async with asyncio.timeout(policy.cleanup_timeout_seconds):
            return await _persist_failure(
                runs=runs, recovery=recovery, policy=policy, intent=intent
            )
    except (TimeoutError, ConversationStoreIntegrityError):
        return _FailureRecord(persistence_pending=True, retry_available=False)
    except ConversationNotFoundError:
        recovery.discard(intent)
        return _FailureRecord(persistence_pending=False, retry_available=False)


async def _persist_failure(
    *, runs: RunLedger, recovery: RecoveryBuffer, policy: TurnPolicy, intent: FailureIntent
) -> _FailureRecord:
    for attempt in range(policy.persistence_attempts):
        try:
            status = await runs.fail_pending(
                subject=intent.subject, run_id=intent.run_id, failure_category=intent.category
            )
        except ConversationStoreUnavailableError:
            if attempt + 1 < policy.persistence_attempts:
                await asyncio.sleep(
                    min(
                        policy.persistence_backoff_seconds * 2**attempt,
                        policy.persistence_max_backoff_seconds,
                    )
                )
        else:
            recovery.discard(intent)
            return _FailureRecord(
                persistence_pending=False, retry_available=status == AttemptStatus.FAILED
            )
    return _FailureRecord(persistence_pending=True, retry_available=False)


async def _cancel(
    *,
    runs: RunLedger,
    recovery: RecoveryBuffer,
    policy: TurnPolicy,
    subject: str,
    admission: Admission,
) -> None:
    """Record a disconnect within the cleanup bound, shielded from the cancelling caller."""
    cleanup = asyncio.create_task(
        _fail(
            runs=runs,
            recovery=recovery,
            policy=policy,
            subject=subject,
            admission=admission,
            category=FailureCategory.CANCELLED,
        )
    )
    try:
        async with asyncio.timeout(policy.cleanup_timeout_seconds):
            await asyncio.shield(cleanup)
    except TimeoutError:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
