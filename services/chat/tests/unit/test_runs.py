"""Run lifecycle rules: terminal transitions, lease ownership and retry availability."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from horizon_chat.domain.runs import (
    AttemptStatus,
    CheckpointPlan,
    CheckpointSource,
    FailureCategory,
    InvalidTerminalStateError,
    Run,
    TerminalTransition,
    TurnStatus,
    checkpoint_start,
    lease_expired,
    lease_held,
    retry_available,
    terminal_transition,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
LATER = NOW + timedelta(seconds=30)
EARLIER = NOW - timedelta(seconds=30)


def attempt(status: AttemptStatus, *, run_id: UUID | None = None) -> Run:
    return Run(
        id=run_id or uuid4(),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        attempt_number=1,
        trace_id=uuid4().hex,
        root_span_id=uuid4().hex[:16],
        status=status,
        failure_category=FailureCategory.INTERNAL if status == AttemptStatus.FAILED else None,
        retry_of_run_id=None,
        started_at=NOW,
        ended_at=None,
        agent_version="test",
        prompt_version="test",
        retrieval_version="test",
    )


def test_completed_attempt_completes_the_turn() -> None:
    assert terminal_transition(
        status=AttemptStatus.COMPLETED, failure_category=None
    ) == TerminalTransition(
        attempt=AttemptStatus.COMPLETED, turn=TurnStatus.COMPLETED, failure_category=None
    )


def test_failed_attempt_fails_the_turn_with_its_category() -> None:
    assert terminal_transition(
        status=AttemptStatus.FAILED, failure_category=FailureCategory.CANCELLED
    ) == TerminalTransition(
        attempt=AttemptStatus.FAILED,
        turn=TurnStatus.FAILED,
        failure_category=FailureCategory.CANCELLED,
    )


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (AttemptStatus.PENDING, None),
        (AttemptStatus.FAILED, None),
        (AttemptStatus.COMPLETED, FailureCategory.INTERNAL),
    ],
)
def test_inconsistent_terminal_requests_are_refused(
    status: AttemptStatus, category: FailureCategory | None
) -> None:
    with pytest.raises(InvalidTerminalStateError):
        terminal_transition(status=status, failure_category=category)


def test_only_the_active_run_with_an_unexpired_lease_holds_it() -> None:
    run_id = uuid4()
    assert lease_held(run_id=run_id, active_run_id=run_id, lease_until=LATER, now=NOW)
    assert not lease_held(run_id=run_id, active_run_id=run_id, lease_until=EARLIER, now=NOW)
    assert not lease_held(run_id=run_id, active_run_id=uuid4(), lease_until=LATER, now=NOW)
    assert not lease_held(run_id=run_id, active_run_id=None, lease_until=None, now=NOW)


def test_an_owned_but_lapsed_lease_is_expired() -> None:
    assert lease_expired(active_run_id=uuid4(), lease_until=EARLIER, now=NOW)
    assert not lease_expired(active_run_id=uuid4(), lease_until=LATER, now=NOW)
    assert not lease_expired(active_run_id=None, lease_until=None, now=NOW)


def test_only_the_newest_failed_attempt_is_retryable() -> None:
    failed = attempt(AttemptStatus.FAILED)
    assert retry_available(attempts=(failed,), latest_run_id=failed.id)
    assert not retry_available(attempts=(failed,), latest_run_id=uuid4())
    completed = attempt(AttemptStatus.COMPLETED)
    assert not retry_available(attempts=(failed, completed), latest_run_id=completed.id)
    assert not retry_available(attempts=(), latest_run_id=None)


@pytest.mark.parametrize(
    ("prepared", "retrying", "predecessor_prepared", "base", "source"),
    [
        pytest.param(True, True, True, None, CheckpointSource.RECORDED, id="already-pinned"),
        pytest.param(False, False, False, None, CheckpointSource.CURRENT, id="first-attempt"),
        pytest.param(
            False, True, False, "base", CheckpointSource.CURRENT, id="unprepared-predecessor"
        ),
        pytest.param(False, True, True, "base", CheckpointSource.PREDECESSOR, id="retry"),
        pytest.param(False, True, True, None, CheckpointSource.EMPTY, id="retry-of-empty-thread"),
    ],
)
def test_checkpoint_start_pins_each_attempt_to_its_pre_input_state(
    prepared: bool,
    retrying: bool,
    predecessor_prepared: bool,
    base: str | None,
    source: CheckpointSource,
) -> None:
    plan = CheckpointPlan(
        conversation_id=uuid4(),
        prepared=prepared,
        base_checkpoint_id=base,
        retrying=retrying,
        predecessor_prepared=predecessor_prepared,
    )
    assert checkpoint_start(plan) == source
