"""Feedback context, crash retries and failure recovery over the real ledger."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_chat.application._turn_stream import prepare_attempt
from horizon_chat.application.recover_failures import RecoveryOutcome, recover_failures
from horizon_chat.db.checkpoints import PostgresCheckpointStore
from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.feedback import FeedbackInput, Rating
from horizon_chat.domain.recovery import FailureIntent, RecoveryBuffer
from horizon_chat.domain.runs import (
    AttemptStatus,
    FailureCategory,
    RunIdentity,
)
from horizon_chat.genai.horizon_agent.runner import attempt_config, attempt_input
from horizon_chat_testing.ledger import Ledger, ledger
from horizon_migrations.db.checkpoints import setup_checkpoints

pytestmark = pytest.mark.integration
GRANTS_SQL = (
    Path(__file__).resolve().parents[4] / "services/migrations/sql/runtime_grants.sql"
).read_text()


@pytest.fixture
async def store(migrated_database: str) -> AsyncIterator[Ledger]:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute(GRANTS_SQL)
    engine = create_async_engine(
        make_url(migrated_database).set(drivername="postgresql+psycopg"),
        connect_args={"options": "-crole=chat_runtime -ctimezone=UTC"},
    )
    try:
        yield ledger(engine)
    finally:
        await engine.dispose()


def identity() -> RunIdentity:
    return RunIdentity(
        id=uuid4(),
        assistant_message_id=uuid4(),
        trace_id=uuid4().hex,
        root_span_id=uuid4().hex[:16],
        agent_version="test-v1",
        prompt_version="test-v1",
        retrieval_version="test-v1",
    )


async def test_feedback_context_and_idempotent_retry(store: Ledger) -> None:
    conversation = await store.conversations.create(subject="alice")
    first = await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="request-1",
        content="question",
        identity=identity(),
    )
    await store.runs.save_partial(subject="alice", run_id=first.run.id, content="Partial")
    await store.runs.finish(
        subject="alice",
        run_id=first.run.id,
        status=AttemptStatus.FAILED,
        content="Partial",
        sources=(),
        failure_category=FailureCategory.SERVICE_UNAVAILABLE,
    )
    retry = await store.runs.retry(
        subject="alice",
        conversation_id=conversation.id,
        turn_id=first.run.turn_id,
        expected_run_id=first.run.id,
        request_key="retry-1",
        identity=identity(),
    )
    replay = await store.runs.retry(
        subject="alice",
        conversation_id=conversation.id,
        turn_id=first.run.turn_id,
        expected_run_id=first.run.id,
        request_key="retry-1",
        identity=identity(),
    )
    assert replay.replayed and replay.run.id == retry.run.id
    assert retry.run.trace_id != first.run.trace_id
    assert retry.run.user_message_id == first.run.user_message_id
    await store.runs.finish(
        subject="alice",
        run_id=retry.run.id,
        status=AttemptStatus.COMPLETED,
        content="Answer",
        sources=(),
        failure_category=None,
    )
    await store.feedback.put_answer_feedback(
        subject="alice",
        message_id=retry.run.assistant_message_id,
        request=FeedbackInput(rating=Rating.DISLIKE, comment=" needs detail "),
    )
    await store.feedback.put_answer_feedback(
        subject="alice",
        message_id=retry.run.assistant_message_id,
        request=FeedbackInput(rating=Rating.LIKE, comment="must be cleared"),
    )
    await store.feedback.put_thread_feedback(
        subject="alice",
        conversation_id=conversation.id,
        request=FeedbackInput(comment="Thread feedback"),
    )
    feedback = (
        await store.conversations.detail(subject="alice", conversation_id=conversation.id)
    ).feedback
    assert feedback is not None and feedback.context_run_id == retry.run.id
    await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="request-2",
        content="follow-up",
        identity=identity(),
    )
    assert (
        await store.conversations.detail(subject="alice", conversation_id=conversation.id)
    ).feedback == feedback
    history = await store.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    assert history.messages[1].status == AttemptStatus.FAILED
    assert history.messages[2].feedback is not None
    assert history.messages[2].feedback.rating == Rating.LIKE
    assert history.messages[2].feedback.comment is None
    await store.feedback.delete_answer_feedback(
        subject="alice", message_id=retry.run.assistant_message_id
    )
    await store.feedback.delete_thread_feedback(subject="alice", conversation_id=conversation.id)
    assert (
        await store.conversations.detail(subject="alice", conversation_id=conversation.id)
    ).feedback is None


@pytest.mark.parametrize("existing_history", [False, True])
async def test_crash_retry_branches_before_input_and_keeps_failed_partial(
    migrated_database: str, store: Ledger, existing_history: bool
) -> None:
    await setup_checkpoints(
        dsn=SecretStr(make_conninfo(migrated_database, options="-crole=checkpoint_migrator")),
        expected_role="checkpoint_migrator",
    )
    # The store fixture applied grants before the checkpoint tables existed.
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as admin:
        await admin.execute(GRANTS_SQL)
    async with await psycopg.AsyncConnection.connect(
        migrated_database,
        autocommit=True,
        row_factory=dict_row,
        options="-crole=chat_runtime -csearch_path=langgraph",
    ) as conn:
        saver = AsyncPostgresSaver(conn)
        checkpoints = PostgresCheckpointStore(saver=saver)
        observed_inputs: list[list[str | list[str | dict[str, object]]]] = []

        async def answer(state: MessagesState) -> MessagesState:
            # A deterministic graph exercises actual Postgres checkpoint history without AWS.
            observed_inputs.append([message.content for message in state["messages"]])
            return {"messages": [AIMessage(content="Graph answer", id=str(uuid4()))]}

        builder = StateGraph(MessagesState)
        builder.add_node("answer", answer)
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile(checkpointer=saver)
        conversation = await store.conversations.create(subject="alice")
        if existing_history:
            await graph.ainvoke(
                {"messages": [HumanMessage(content="Earlier question", id="earlier")]},
                {"configurable": {"thread_id": str(conversation.id)}},
            )
        first = await store.runs.admit(
            subject="alice",
            conversation_id=conversation.id,
            request_key="crash",
            content="Current question",
            identity=identity(),
        )
        with pytest.raises(ConversationNotFoundError):
            await prepare_attempt(
                subject="bob", run_id=first.run.id, runs=store.runs, checkpoints=checkpoints
            )
        start = await prepare_attempt(
            subject="alice", run_id=first.run.id, runs=store.runs, checkpoints=checkpoints
        )
        await graph.ainvoke(attempt_input(first), attempt_config(start))
        await store.runs.save_partial(
            subject="alice", run_id=first.run.id, content="Partial answer"
        )
        # Simulate a dead process after graph writes, without ledger completion or cleanup.
        async with await psycopg.AsyncConnection.connect(
            migrated_database, autocommit=True
        ) as admin:
            await admin.execute(
                "UPDATE app.conversations SET lease_until = now() - interval '1 second' WHERE id = %s",
                (conversation.id,),
            )
        recovered = await store.conversations.turn(
            subject="alice", conversation_id=conversation.id, turn_id=first.run.turn_id
        )
        assert recovered.attempts[0].failure_category == FailureCategory.INTERRUPTED
        retry = await store.runs.retry(
            subject="alice",
            conversation_id=conversation.id,
            turn_id=first.run.turn_id,
            expected_run_id=first.run.id,
            request_key="retry-crash",
            identity=identity(),
        )
        retry_start = await prepare_attempt(
            subject="alice", run_id=retry.run.id, runs=store.runs, checkpoints=checkpoints
        )
        assert retry_start.checkpoint_id == start.checkpoint_id
        result = await graph.ainvoke(attempt_input(retry), attempt_config(retry_start))
        expected = ["Earlier question", "Graph answer"] if existing_history else []
        assert observed_inputs[-1] == [*expected, "Current question"]
        assert (
            sum(message.id == str(first.run.user_message_id) for message in result["messages"]) == 1
        )
        await store.runs.finish(
            subject="alice",
            run_id=retry.run.id,
            status=AttemptStatus.COMPLETED,
            content="Graph answer",
            sources=(),
            failure_category=None,
        )
        history = await store.conversations.history(
            subject="alice", conversation_id=conversation.id, cursor=0, limit=100
        )
        assert [message.status for message in history.messages] == [
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.COMPLETED,
        ]
        assert history.messages[1].content == "Partial answer"
        assert history.messages[1].trace_id == first.run.trace_id
        assert retry.run.user_message_id == first.run.user_message_id


async def test_recovery_continues_past_an_inconsistent_run(store: Ledger) -> None:
    broken = await store.conversations.create(subject="alice")
    healthy = await store.conversations.create(subject="alice")
    first = await store.runs.admit(
        subject="alice",
        conversation_id=broken.id,
        request_key="broken",
        content="Horizon",
        identity=identity(),
    )
    second = await store.runs.admit(
        subject="alice",
        conversation_id=healthy.id,
        request_key="healthy",
        content="Horizon",
        identity=identity(),
    )
    async with store.engine.begin() as conn:
        # The pending run lost its lease row: applying its failure intent violates the ledger.
        await conn.execute(
            text(
                "UPDATE app.conversations SET active_run_id = NULL, lease_until = NULL"
                " WHERE id = :id"
            ),
            {"id": broken.id},
        )
    buffer = RecoveryBuffer()
    for admission in (first, second):
        buffer.add(
            FailureIntent(
                subject="alice",
                conversation_id=admission.run.conversation_id,
                run_id=admission.run.id,
                category=FailureCategory.CANCELLED,
            )
        )
    outcome = await recover_failures(buffer=buffer, store=store.runs)
    assert outcome == RecoveryOutcome(repaired=1, failed=1)
    assert buffer.pending == {}
    turn = await store.conversations.turn(
        subject="alice", conversation_id=healthy.id, turn_id=second.run.turn_id
    )
    assert turn.attempts[-1].failure_category == FailureCategory.CANCELLED
    other = await store.conversations.create(subject="alice")
    admitted = await store.runs.admit(
        subject="alice",
        conversation_id=other.id,
        request_key="after",
        content="Horizon",
        identity=identity(),
    )
    assert not admitted.replayed
