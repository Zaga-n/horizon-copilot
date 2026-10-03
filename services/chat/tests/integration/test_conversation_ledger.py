"""Ownership, admission races and immutable execution joins over the real ledger."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.runs import (
    AttemptStatus,
    ConversationBusyError,
    IdempotencyConflictError,
    RunIdentity,
)
from horizon_chat_testing.ledger import Ledger, ledger

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


async def test_title_uses_first_saved_question_for_list_and_detail(store: Ledger) -> None:
    conversation = await store.conversations.create(subject="alice")
    assert conversation.title == "New conversation"
    first = await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="first",
        content="  Τι ξέρεις\n για τα Horizon;  ",  # noqa: RUF001 -- Greek user question
        identity=identity(),
    )
    await store.runs.finish(
        subject="alice",
        run_id=first.run.id,
        status=AttemptStatus.COMPLETED,
        content="Answer",
        sources=(),
        failure_category=None,
    )
    await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="second",
        content="Another question",
        identity=identity(),
    )
    detail = await store.conversations.detail(subject="alice", conversation_id=conversation.id)
    assert detail.title == "Τι ξέρεις για τα Horizon;"  # noqa: RUF001 -- Greek title
    page = await store.conversations.list(subject="alice", cursor=None, limit=100)
    assert page.conversations[0].title == "Τι ξέρεις για τα Horizon;"  # noqa: RUF001 -- Greek title


async def test_duplicate_submission_and_owner_isolation(store: Ledger) -> None:
    conversation = await store.conversations.create(subject="alice")
    accepted = await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="request-1",
        content="Horizon question",
        identity=identity(),
    )
    replay = await store.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="request-1",
        content="Horizon question",
        identity=identity(),
    )
    assert replay.replayed and replay.run.id == accepted.run.id
    with pytest.raises(IdempotencyConflictError):
        await store.runs.admit(
            subject="alice",
            conversation_id=conversation.id,
            request_key="request-1",
            content="different",
            identity=identity(),
        )
    with pytest.raises(ConversationNotFoundError):
        await store.conversations.history(
            subject="bob", conversation_id=conversation.id, cursor=0, limit=100
        )
    assert not (await store.conversations.list(subject="bob", cursor=None, limit=100)).conversations
    history = await store.conversations.history(
        subject="alice", conversation_id=conversation.id, cursor=0, limit=100
    )
    assert len(history.messages) == 2
    assert history.messages[1].run_id == accepted.run.id
    assert history.messages[1].trace_id == accepted.run.trace_id
    offset = history.messages[0].created_at.utcoffset()
    assert offset is not None and offset.total_seconds() == 0


async def test_concurrent_turns_have_one_winner(store: Ledger) -> None:
    conversation = await store.conversations.create(subject="alice")
    results = await asyncio.gather(
        *[
            store.runs.admit(
                subject="alice",
                conversation_id=conversation.id,
                request_key=f"request-{i}",
                content="question",
                identity=identity(),
            )
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ConversationBusyError) for result in results) == 1
    assert (
        len(
            (
                await store.conversations.history(
                    subject="alice", conversation_id=conversation.id, cursor=0, limit=100
                )
            ).messages
        )
        == 2
    )


async def test_runtime_roles_cannot_cross_storage_boundaries(
    migrated_database: str, store: Ledger
) -> None:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute("SET ROLE chat_runtime")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("CREATE TABLE app.forbidden_ddl (id integer)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM app.documents")
        await conn.execute("RESET ROLE")
        await conn.execute("SET ROLE ingestion_runtime")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("SELECT content FROM app.messages")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("CREATE TABLE langgraph.forbidden_ddl (id integer)")
