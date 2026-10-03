"""Owned conversation creation and reads; reads repair an expired run first."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_chat.db.ledger import (
    database_now,
    owned_conversation,
    recover_expired_run,
    run_from_row,
)
from horizon_chat.db.tables import (
    CONVERSATIONS,
    DOCUMENTS,
    MESSAGE_FEEDBACK,
    MESSAGES,
    RUNS,
    THREAD_FEEDBACK,
    TURNS,
    USERS,
    VERSIONS,
)
from horizon_chat.db.transactions import transaction
from horizon_chat.domain.conversations import (
    Conversation,
    ConversationNotFoundError,
    ConversationPage,
    ConversationStatus,
    HistoryPage,
    Message,
)
from horizon_chat.domain.runs import MessageRole, Turn, retry_available

CONVERSATION_FIELDS = ("id", "status", "created_at", "updated_at", "last_activity_at")
# Deriving titles from saved questions also names existing conversations without a backfill.
FIRST_QUESTION = (
    select(MESSAGES.c.content)
    .where(
        MESSAGES.c.conversation_id == CONVERSATIONS.c.id,
        MESSAGES.c.role == MessageRole.USER,
    )
    .order_by(MESSAGES.c.message_order)
    .limit(1)
    .correlate(CONVERSATIONS)
    .scalar_subquery()
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SqlConversationStore:
    engine: AsyncEngine
    id_factory: Callable[[], UUID] = uuid4

    async def create(self, *, subject: str) -> Conversation:
        async with transaction(self.engine) as conn:
            await conn.execute(
                insert(USERS)
                .values(id=self.id_factory(), subject=subject)
                .on_conflict_do_nothing(index_elements=[USERS.c.subject])
            )
            user_id = await conn.scalar(select(USERS.c.id).where(USERS.c.subject == subject))
            row = (
                (
                    await conn.execute(
                        insert(CONVERSATIONS)
                        .values(id=self.id_factory(), user_id=user_id)
                        .returning(*[CONVERSATIONS.c[name] for name in CONVERSATION_FIELDS])
                    )
                )
                .mappings()
                .one()
            )
            return Conversation.model_validate(row)

    async def list(self, *, subject: str, cursor: UUID | None, limit: int) -> ConversationPage:
        async with transaction(self.engine) as conn:
            query = (
                select(
                    *[CONVERSATIONS.c[name] for name in CONVERSATION_FIELDS],
                    FIRST_QUESTION.label("title"),
                )
                .join(USERS)
                .where(
                    USERS.c.subject == subject, CONVERSATIONS.c.status == ConversationStatus.ACTIVE
                )
            )
            if cursor is not None:
                row = await owned_conversation(conn, subject=subject, conversation_id=cursor)
                query = query.where(
                    tuple_(CONVERSATIONS.c.created_at, CONVERSATIONS.c.id)
                    < tuple_(literal(row["created_at"]), literal(cursor))
                )
            rows = (
                (
                    await conn.execute(
                        query.order_by(
                            CONVERSATIONS.c.created_at.desc(), CONVERSATIONS.c.id.desc()
                        ).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = tuple(Conversation.model_validate(row) for row in rows[:limit])
            return ConversationPage(
                conversations=items, next_cursor=items[-1].id if len(rows) > limit else None
            )

    async def detail(self, *, subject: str, conversation_id: UUID) -> Conversation:
        async with transaction(self.engine) as conn:
            row = await owned_conversation(conn, subject=subject, conversation_id=conversation_id)
            await recover_expired_run(conn, row, await database_now(conn))
            title = await conn.scalar(
                select(FIRST_QUESTION).where(CONVERSATIONS.c.id == conversation_id)
            )
            feedback = (
                (
                    await conn.execute(
                        select(
                            *[
                                THREAD_FEEDBACK.c[name]
                                for name in (
                                    "rating",
                                    "comment",
                                    "created_at",
                                    "updated_at",
                                    "context_run_id",
                                    "context_message_order",
                                )
                            ]
                        ).where(
                            THREAD_FEEDBACK.c.conversation_id == conversation_id,
                            THREAD_FEEDBACK.c.user_id == row["user_id"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            return Conversation.model_validate(
                {
                    **{name: row[name] for name in CONVERSATION_FIELDS},
                    "feedback": feedback,
                    "title": title,
                }
            )

    async def history(
        self, *, subject: str, conversation_id: UUID, cursor: int, limit: int
    ) -> HistoryPage:
        async with transaction(self.engine) as conn:
            row = await owned_conversation(conn, subject=subject, conversation_id=conversation_id)
            await recover_expired_run(conn, row, await database_now(conn))
            records = (
                (
                    await conn.execute(
                        select(MESSAGES, RUNS.c.id.label("run_id"), RUNS.c.trace_id)
                        .outerjoin(RUNS, RUNS.c.assistant_message_id == MESSAGES.c.id)
                        .where(
                            MESSAGES.c.conversation_id == conversation_id,
                            MESSAGES.c.message_order > cursor,
                        )
                        .order_by(MESSAGES.c.message_order)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            messages: list[Message] = []
            for record in records[:limit]:
                feedback = (
                    (
                        await conn.execute(
                            select(
                                *[
                                    MESSAGE_FEEDBACK.c[name]
                                    for name in ("rating", "comment", "created_at", "updated_at")
                                ]
                            ).where(
                                MESSAGE_FEEDBACK.c.assistant_message_id == record["id"],
                                MESSAGE_FEEDBACK.c.user_id == row["user_id"],
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                messages.append(Message.model_validate({**record, "feedback": feedback}))
            version_ids = {
                source.document_version_id for message in messages for source in message.sources
            }
            available = (
                set(
                    await conn.scalars(
                        select(VERSIONS.c.id)
                        .join(DOCUMENTS, VERSIONS.c.document_id == DOCUMENTS.c.id)
                        .where(
                            VERSIONS.c.id.in_(version_ids),
                            VERSIONS.c.object_key.is_not(None),
                            VERSIONS.c.retired_at.is_(None),
                            DOCUMENTS.c.lifecycle == "live",
                        )
                    )
                )
                if version_ids
                else set()
            )
            messages = [
                message.model_copy(
                    update={
                        "sources": tuple(
                            source.model_copy(
                                update={"unavailable": source.document_version_id not in available}
                            )
                            for source in message.sources
                        )
                    }
                )
                for message in messages
            ]
            return HistoryPage(
                messages=tuple(messages),
                next_cursor=messages[-1].message_order if len(records) > limit else None,
            )

    async def turn(self, *, subject: str, conversation_id: UUID, turn_id: UUID) -> Turn:
        async with transaction(self.engine) as conn:
            conversation = await owned_conversation(
                conn, subject=subject, conversation_id=conversation_id
            )
            await recover_expired_run(conn, conversation, await database_now(conn))
            row = (
                (
                    await conn.execute(
                        select(
                            TURNS.c.id,
                            TURNS.c.conversation_id,
                            TURNS.c.user_message_id,
                            TURNS.c.status,
                        ).where(TURNS.c.id == turn_id, TURNS.c.conversation_id == conversation_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ConversationNotFoundError()
            runs = (
                (
                    await conn.execute(
                        select(RUNS)
                        .where(RUNS.c.turn_id == turn_id)
                        .order_by(RUNS.c.attempt_number)
                    )
                )
                .mappings()
                .all()
            )
            latest_id = await conn.scalar(
                select(RUNS.c.id)
                .join(MESSAGES, RUNS.c.assistant_message_id == MESSAGES.c.id)
                .where(RUNS.c.conversation_id == conversation_id)
                .order_by(MESSAGES.c.message_order.desc())
                .limit(1)
            )
            attempts = tuple(run_from_row(run) for run in runs)
            return Turn(
                id=row["id"],
                conversation_id=row["conversation_id"],
                user_message_id=row["user_message_id"],
                status=row["status"],
                attempts=attempts,
                retry_available=retry_available(
                    attempts=attempts,
                    latest_run_id=latest_id if isinstance(latest_id, UUID) else None,
                ),
            )
