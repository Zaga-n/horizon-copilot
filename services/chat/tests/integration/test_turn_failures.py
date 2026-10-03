"""Turn failure persistence: disconnects, outages and uncertain completion commits."""

import asyncio
from uuid import UUID

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

from horizon_chat.application.recover_failures import recover_failures
from horizon_chat.db.runs import SqlRunLedger
from horizon_chat.domain.runs import AttemptStatus, FailureCategory
from horizon_chat.ports.conversations import ConversationStoreUnavailableError
from horizon_chat_testing.streaming import (
    ControlledAgent,
    StreamingRuntime,
    admit,
)

pytestmark = pytest.mark.integration


async def test_prefix_is_durable_before_visible_and_disconnect_fails_it(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
) -> None:
    runtime, agent, _ = streaming
    agent.gate = asyncio.Event()
    conversation = await runtime.conversations.create(subject="alice")
    admission, stream = await admit(
        runtime,
        subject="alice",
        conversation_id=conversation.id,
        request_key="cancel",
        content="Question",
    )
    assert (await anext(stream)).event == "started"
    assert (await anext(stream)).data.model_dump()["text"] == "Visible prefix. "
    history = await runtime.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    assert history.messages[-1].content == "Visible prefix. "
    task = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn = await runtime.conversations.turn(
        subject="alice", conversation_id=conversation.id, turn_id=admission.run.turn_id
    )
    assert turn.attempts[-1].status == AttemptStatus.FAILED


async def test_outage_emits_pending_and_recovers_without_downgrading_completion(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, agent, _ = streaming
    agent.fail = True
    conversation = await runtime.conversations.create(subject="alice")
    _, stream = await admit(
        runtime,
        subject="alice",
        conversation_id=conversation.id,
        request_key="outage",
        content="Question",
    )
    original = SqlRunLedger.fail_pending
    calls = 0

    async def unavailable(self: SqlRunLedger, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise ConversationStoreUnavailableError("PRIVATE DB credential")

    monkeypatch.setattr(SqlRunLedger, "fail_pending", unavailable)
    emitted = [item async for item in stream]
    assert emitted[-1].event == "failed" and emitted[-1].data.model_dump()["persistence_pending"]
    assert not emitted[-1].data.model_dump()["retry_available"] and calls == 2
    monkeypatch.setattr(SqlRunLedger, "fail_pending", original)
    outcome = await recover_failures(buffer=runtime.recovery, store=runtime.runs)
    assert outcome.repaired == 1
    history = await runtime.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    assert history.messages[-1].status == AttemptStatus.FAILED
    assert history.messages[-1].content == "Visible prefix. "


async def test_uncertain_completion_commit_is_never_downgraded_to_failed(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from horizon_chat.domain.runs import FailureCategory
    from horizon_chat.domain.sources import Source

    runtime, _, _ = streaming
    conversation = await runtime.conversations.create(subject="alice")
    admission, stream = await admit(
        runtime,
        subject="alice",
        conversation_id=conversation.id,
        request_key="commit-loss",
        content="Question",
    )
    original = SqlRunLedger.finish

    async def commit_then_disconnect(
        self: SqlRunLedger,
        *,
        subject: str,
        run_id: UUID,
        status: AttemptStatus,
        content: str,
        sources: tuple[Source, ...],
        failure_category: FailureCategory | None,
    ) -> None:
        await original(
            self,
            subject=subject,
            run_id=run_id,
            status=status,
            content=content,
            sources=sources,
            failure_category=failure_category,
        )
        raise ConversationStoreUnavailableError("lost acknowledgement")

    monkeypatch.setattr(SqlRunLedger, "finish", commit_then_disconnect)
    emitted = [item async for item in stream]
    assert emitted[-1].event == "failed"
    assert emitted[-1].data.model_dump()["retry_available"] is False
    authoritative = await runtime.conversations.turn(
        subject="alice", conversation_id=conversation.id, turn_id=admission.run.turn_id
    )
    assert authoritative.attempts[-1].status == AttemptStatus.COMPLETED
    assert not authoritative.retry_available
    history = await runtime.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    assert history.messages[-1].status == AttemptStatus.COMPLETED
    assert history.messages[-1].content == "Visible prefix. Final answer."


async def test_a_stream_that_outlives_its_lease_ends_as_interrupted(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
) -> None:
    runtime, agent, _ = streaming
    agent.gate = asyncio.Event()
    conversation = await runtime.conversations.create(subject="alice")
    admission, stream = await admit(
        runtime,
        subject="alice",
        conversation_id=conversation.id,
        request_key="slow-client",
        content="Question",
    )
    assert (await anext(stream)).event == "started"
    assert (await anext(stream)).event == "delta"
    async with runtime.runs.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.conversations SET lease_until = clock_timestamp() - interval '1 second'"
                " WHERE id = :id"
            ),
            {"id": conversation.id},
        )
    # Another request takes the conversation over, failing the expired run as interrupted.
    takeover, _ = await admit(
        runtime,
        subject="alice",
        conversation_id=conversation.id,
        request_key="takeover",
        content="Follow-up",
    )
    agent.gate.set()
    rest = [item async for item in stream]
    assert rest[-1].event == "failed"
    assert rest[-1].data.model_dump()["failure_category"] == FailureCategory.INTERRUPTED
    interrupted = await runtime.conversations.turn(
        subject="alice", conversation_id=conversation.id, turn_id=admission.run.turn_id
    )
    assert interrupted.attempts[-1].status == AttemptStatus.FAILED
    assert interrupted.attempts[-1].failure_category == FailureCategory.INTERRUPTED
    history = await runtime.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    late = next(m for m in history.messages if m.id == admission.run.assistant_message_id)
    assert late.content == "Visible prefix. "
    async with runtime.runs.engine.connect() as conn:
        active = await conn.scalar(
            text("SELECT active_run_id FROM app.conversations WHERE id = :id"),
            {"id": conversation.id},
        )
    assert active == takeover.run.id
