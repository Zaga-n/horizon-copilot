"""The locked reads and writes every conversation-ledger store shares."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from horizon_chat.db.tables import CONVERSATIONS, MESSAGES, RUNS, TURNS, USERS
from horizon_chat.domain.conversations import (
    ConversationNotFoundError,
    ConversationStatus,
    ensure_available,
)
from horizon_chat.domain.runs import (
    ADMISSION,
    AttemptStatus,
    ConversationBusyError,
    FailureCategory,
    MessageRole,
    Run,
    RunIdentity,
    TerminalTransition,
    lease_expired,
    lease_held,
    terminal_transition,
)
from horizon_chat.domain.sources import Source
from horizon_chat.ports.conversations import ConversationStoreIntegrityError

RUN_FIELDS = tuple(Run.model_fields)


def run_from_row(row: RowMapping) -> Run:
    return Run.model_validate({name: row[name] for name in RUN_FIELDS})


async def database_now(conn: AsyncConnection) -> datetime:
    now = await conn.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):
        raise ConversationStoreIntegrityError("database_clock")
    return now


async def end_attempt(
    conn: AsyncConnection,
    *,
    run: RowMapping,
    transition: TerminalTransition,
    now: datetime,
    content: str | None = None,
    sources: tuple[Source, ...] | None = None,
) -> None:
    """The one writer of an ended attempt: run, assistant message, turn, and lease release.

    Only a still-pending run and message change; `None` content or sources keep what the
    attempt already persisted.
    """
    message = (
        update(MESSAGES)
        .where(
            MESSAGES.c.id == run["assistant_message_id"],
            MESSAGES.c.status == AttemptStatus.PENDING,
        )
        .values(status=transition.attempt, updated_at=now)
    )
    if content is not None:
        message = message.values(content=content)
    if sources is not None:
        message = message.values(sources=[source.model_dump(mode="json") for source in sources])
    await conn.execute(message)
    await conn.execute(
        update(RUNS)
        .where(RUNS.c.id == run["id"], RUNS.c.status == AttemptStatus.PENDING)
        .values(
            status=transition.attempt,
            ended_at=now,
            failure_category=transition.failure_category,
        )
    )
    await conn.execute(
        update(TURNS)
        .where(TURNS.c.id == run["turn_id"])
        .values(status=transition.turn, updated_at=now)
    )
    await conn.execute(
        update(CONVERSATIONS)
        .where(CONVERSATIONS.c.id == run["conversation_id"])
        .values(active_run_id=None, lease_until=None, updated_at=now)
    )


async def recover_expired_run(conn: AsyncConnection, row: RowMapping, now: datetime) -> None:
    if not lease_expired(
        active_run_id=row["active_run_id"], lease_until=row["lease_until"], now=now
    ):
        return
    run = (
        (await conn.execute(select(RUNS).where(RUNS.c.id == row["active_run_id"]))).mappings().one()
    )
    await end_attempt(
        conn,
        run=run,
        transition=terminal_transition(
            status=AttemptStatus.FAILED, failure_category=FailureCategory.INTERRUPTED
        ),
        now=now,
    )


async def owned_conversation(
    conn: AsyncConnection, *, subject: str, conversation_id: UUID
) -> RowMapping:
    """Lock the subject's conversation row; purging conversations are not found."""
    row = (
        (
            await conn.execute(
                select(CONVERSATIONS)
                .join(USERS)
                .where(
                    CONVERSATIONS.c.id == conversation_id,
                    USERS.c.subject == subject,
                )
                .with_for_update(of=CONVERSATIONS)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ConversationNotFoundError()
    ensure_available(status=ConversationStatus(row["status"]))
    return row


async def owned_run(
    conn: AsyncConnection, *, subject: str, run_id: UUID
) -> tuple[RowMapping, RowMapping]:
    """Lock the run's conversation for its owner, then read the run under that lock."""
    record = (await conn.execute(select(RUNS).where(RUNS.c.id == run_id))).mappings().one_or_none()
    if record is None:
        raise ConversationNotFoundError()
    conversation = await owned_conversation(
        conn, subject=subject, conversation_id=record["conversation_id"]
    )
    record = (await conn.execute(select(RUNS).where(RUNS.c.id == run_id))).mappings().one()
    return conversation, record


async def held_run(
    conn: AsyncConnection, *, subject: str, run_id: UUID
) -> tuple[RowMapping, RowMapping, datetime]:
    """The locked conversation and run while `run_id` still holds the lease; else busy."""
    conversation, run = await owned_run(conn, subject=subject, run_id=run_id)
    now = await database_now(conn)
    if not lease_held(
        run_id=run_id,
        active_run_id=conversation["active_run_id"],
        lease_until=conversation["lease_until"],
        now=now,
    ):
        raise ConversationBusyError()
    return conversation, run, now


async def reserve_run(
    conn: AsyncConnection,
    *,
    turn_lease_seconds: float,
    conversation: RowMapping,
    turn_id: UUID,
    user_message_id: UUID,
    identity: RunIdentity,
    attempt_number: int,
    retry_of_run_id: UUID | None,
    message_order: int,
    now: datetime,
) -> Run:
    """Reserve the pending assistant message and run, and lease the conversation to it."""
    await conn.execute(
        insert(MESSAGES).values(
            id=identity.assistant_message_id,
            conversation_id=conversation["id"],
            turn_id=turn_id,
            attempt_number=attempt_number,
            role=MessageRole.ASSISTANT,
            status=ADMISSION.attempt,
            message_order=message_order,
        )
    )
    row = (
        (
            await conn.execute(
                insert(RUNS)
                .values(
                    id=identity.id,
                    conversation_id=conversation["id"],
                    turn_id=turn_id,
                    user_message_id=user_message_id,
                    assistant_message_id=identity.assistant_message_id,
                    attempt_number=attempt_number,
                    trace_id=identity.trace_id,
                    root_span_id=identity.root_span_id,
                    agent_version=identity.agent_version,
                    prompt_version=identity.prompt_version,
                    retrieval_version=identity.retrieval_version,
                    status=ADMISSION.attempt,
                    retry_of_run_id=retry_of_run_id,
                )
                .returning(RUNS)
            )
        )
        .mappings()
        .one()
    )
    await conn.execute(
        update(CONVERSATIONS)
        .where(CONVERSATIONS.c.id == conversation["id"])
        .values(
            active_run_id=identity.id,
            lease_until=now + timedelta(seconds=turn_lease_seconds),
            next_message_order=message_order + 1,
            last_activity_at=now,
            updated_at=now,
        )
    )
    return run_from_row(row)
