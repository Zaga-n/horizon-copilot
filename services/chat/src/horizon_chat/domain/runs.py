"""Turn attempts: admission, lease, retry and terminal-transition rules."""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from horizon_chat.domain.values import StrictModel

MAX_REQUEST_KEY_CHARS = 200


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class TurnStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class FailureCategory(StrEnum):
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SERVICE_UNAVAILABLE = "service_unavailable"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_PROTOCOL = "provider_protocol"
    INDEX_INTEGRITY = "index_integrity"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_CITATIONS = "invalid_citations"
    INTERNAL = "internal"


class ConversationBusyError(Exception):
    """An unexpired run already owns the conversation, or this run no longer holds it."""


class IdempotencyConflictError(Exception):
    """A request key was previously used for different business input."""


class StaleRetryError(Exception):
    """The failed attempt is no longer the conversation's latest accepted run."""


class InvalidTerminalStateError(Exception):
    """A caller asked to end an attempt as pending, or with a category that does not fit."""


class Run(StrictModel):
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    attempt_number: int
    trace_id: str
    base_checkpoint_id: str | None = Field(default=None, exclude=True)
    checkpoint_prepared: bool = Field(default=False, exclude=True)
    root_span_id: str
    status: AttemptStatus
    failure_category: FailureCategory | None
    retry_of_run_id: UUID | None
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    agent_version: str
    prompt_version: str
    retrieval_version: str


class Turn(StrictModel):
    id: UUID
    conversation_id: UUID
    user_message_id: UUID
    status: TurnStatus
    attempts: tuple[Run, ...]
    retry_available: bool = False


class RunIdentity(StrictModel):
    id: UUID
    assistant_message_id: UUID
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    root_span_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
    agent_version: Annotated[str, Field(min_length=1, max_length=64)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=64)]
    retrieval_version: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("trace_id", "root_span_id")
    @classmethod
    def nonzero_trace_identity(cls, value: str) -> str:
        if int(value, 16) == 0:
            raise ValueError("trace and span identities must be nonzero")
        return value


class Admission(StrictModel):
    run: Run
    input: str
    replayed: bool


class CheckpointPlan(StrictModel):
    conversation_id: UUID
    prepared: bool
    base_checkpoint_id: str | None
    retrying: bool
    predecessor_prepared: bool


class CheckpointStart(StrictModel):
    conversation_id: UUID
    checkpoint_id: str | None


class CheckpointSource(StrEnum):
    """Where an attempt's graph starts; every source but RECORDED is recorded once."""

    RECORDED = "recorded"  # this attempt already pinned its base checkpoint
    CURRENT = "current"  # a first attempt pins the thread's current state
    PREDECESSOR = "predecessor"  # a retry restarts from the base its failed attempt used
    EMPTY = "empty"  # that base was an empty thread, so all current state is failed work


def checkpoint_start(plan: CheckpointPlan) -> CheckpointSource:
    if plan.prepared:
        return CheckpointSource.RECORDED
    if not plan.retrying or not plan.predecessor_prepared:
        return CheckpointSource.CURRENT
    if plan.base_checkpoint_id is None:
        return CheckpointSource.EMPTY
    return CheckpointSource.PREDECESSOR


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnPolicy:
    """Versions recorded on every attempt and the bounded retry of its failure record."""

    agent_version: str
    prompt_version: str
    retrieval_version: str
    persistence_attempts: int
    persistence_backoff_seconds: float
    persistence_max_backoff_seconds: float
    cleanup_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.persistence_attempts < 1:
            raise ValueError("persistence_attempts must be at least 1")
        if self.persistence_backoff_seconds > self.persistence_max_backoff_seconds:
            raise ValueError("persistence_backoff_seconds exceeds the maximum backoff")
        if self.cleanup_timeout_seconds <= 0:
            raise ValueError("cleanup_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionStates:
    """An admitted turn is active, its user message complete and its new attempt pending."""

    turn: TurnStatus
    user_message: AttemptStatus
    attempt: AttemptStatus


ADMISSION = AdmissionStates(
    turn=TurnStatus.ACTIVE, user_message=AttemptStatus.COMPLETED, attempt=AttemptStatus.PENDING
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalTransition:
    """The statuses one ended attempt writes to its run, assistant message and turn."""

    attempt: AttemptStatus
    turn: TurnStatus
    failure_category: FailureCategory | None


def terminal_transition(
    *, status: AttemptStatus, failure_category: FailureCategory | None
) -> TerminalTransition:
    """A completed attempt completes its turn; a failed one needs and records a category."""
    if status == AttemptStatus.PENDING or (status == AttemptStatus.FAILED) != (
        failure_category is not None
    ):
        raise InvalidTerminalStateError()
    return TerminalTransition(
        attempt=status,
        turn=TurnStatus.COMPLETED if status == AttemptStatus.COMPLETED else TurnStatus.FAILED,
        failure_category=failure_category,
    )


def lease_active(*, lease_until: datetime | None, now: datetime) -> bool:
    return lease_until is not None and lease_until > now


def lease_held(
    *, run_id: UUID, active_run_id: UUID | None, lease_until: datetime | None, now: datetime
) -> bool:
    """Whether `run_id` still owns the conversation; only its holder may write results."""
    return active_run_id == run_id and lease_active(lease_until=lease_until, now=now)


def lease_expired(
    *, active_run_id: UUID | None, lease_until: datetime | None, now: datetime
) -> bool:
    """A run owns the conversation but its lease lapsed, so the run was interrupted."""
    return active_run_id is not None and not lease_active(lease_until=lease_until, now=now)


def retry_available(*, attempts: Sequence[Run], latest_run_id: UUID | None) -> bool:
    """Only the conversation's newest attempt, once failed, may be retried."""
    return bool(
        attempts
        and attempts[-1].status == AttemptStatus.FAILED
        and attempts[-1].id == latest_run_id
    )


def input_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ensure_idle(*, lease_until: datetime | None, now: datetime) -> None:
    if lease_active(lease_until=lease_until, now=now):
        raise ConversationBusyError()


def ensure_same_input(*, stored_hash: str, content: str) -> None:
    if stored_hash != input_hash(content):
        raise IdempotencyConflictError()


def ensure_retryable(*, latest: Run, expected_run_id: UUID, turn_id: UUID) -> None:
    if (
        latest.id != expected_run_id
        or latest.turn_id != turn_id
        or latest.status != AttemptStatus.FAILED
    ):
        raise StaleRetryError()
