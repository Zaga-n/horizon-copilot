"""Answer and thread feedback owned by the conversation's subject."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import RowMapping, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from horizon_chat.db.ledger import database_now, owned_conversation, recover_expired_run
from horizon_chat.db.tables import MESSAGE_FEEDBACK, MESSAGES, RUNS, THREAD_FEEDBACK
from horizon_chat.db.transactions import transaction
from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.feedback import FeedbackInput, answer_feedback
from horizon_chat.domain.runs import AttemptStatus, MessageRole


@dataclass(frozen=True, slots=True, kw_only=True)
class SqlFeedbackStore:
    engine: AsyncEngine

    async def _feedback_message(
        self, conn: AsyncConnection, *, subject: str, message_id: UUID
    ) -> tuple[RowMapping, RowMapping]:
        message = (
            (await conn.execute(select(MESSAGES).where(MESSAGES.c.id == message_id)))
            .mappings()
            .one_or_none()
        )
        if message is None:
            raise ConversationNotFoundError()
        conversation = await owned_conversation(
            conn, subject=subject, conversation_id=message["conversation_id"]
        )
        return conversation, message

    async def put_answer_feedback(
        self, *, subject: str, message_id: UUID, request: FeedbackInput
    ) -> None:
        async with transaction(self.engine) as conn:
            conversation, message = await self._feedback_message(
                conn, subject=subject, message_id=message_id
            )
            value = answer_feedback(
                status=AttemptStatus(message["status"]),
                role=MessageRole(message["role"]),
                request=request,
            )
            now = await database_now(conn)
            stmt = insert(MESSAGE_FEEDBACK).values(
                user_id=conversation["user_id"],
                assistant_message_id=message_id,
                conversation_id=conversation["id"],
                rating=value.rating,
                comment=value.comment,
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[
                        MESSAGE_FEEDBACK.c.user_id,
                        MESSAGE_FEEDBACK.c.assistant_message_id,
                    ],
                    set_={"rating": value.rating, "comment": value.comment, "updated_at": now},
                )
            )

    async def delete_answer_feedback(self, *, subject: str, message_id: UUID) -> None:
        async with transaction(self.engine) as conn:
            conversation, _ = await self._feedback_message(
                conn, subject=subject, message_id=message_id
            )
            await conn.execute(
                delete(MESSAGE_FEEDBACK).where(
                    MESSAGE_FEEDBACK.c.user_id == conversation["user_id"],
                    MESSAGE_FEEDBACK.c.assistant_message_id == message_id,
                )
            )

    async def put_thread_feedback(
        self, *, subject: str, conversation_id: UUID, request: FeedbackInput
    ) -> None:
        async with transaction(self.engine) as conn:
            conversation = await owned_conversation(
                conn, subject=subject, conversation_id=conversation_id
            )
            await recover_expired_run(conn, conversation, await database_now(conn))
            latest_id = await conn.scalar(
                select(RUNS.c.id)
                .join(MESSAGES, RUNS.c.assistant_message_id == MESSAGES.c.id)
                .where(RUNS.c.conversation_id == conversation_id)
                .order_by(MESSAGES.c.message_order.desc())
                .limit(1)
            )
            boundary = conversation["next_message_order"] - 1
            now = await database_now(conn)
            stmt = insert(THREAD_FEEDBACK).values(
                user_id=conversation["user_id"],
                conversation_id=conversation_id,
                rating=request.rating,
                comment=request.comment,
                context_run_id=latest_id,
                context_message_order=boundary,
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[THREAD_FEEDBACK.c.user_id, THREAD_FEEDBACK.c.conversation_id],
                    set_={
                        "rating": request.rating,
                        "comment": request.comment,
                        "context_run_id": latest_id,
                        "context_message_order": boundary,
                        "updated_at": now,
                    },
                )
            )

    async def delete_thread_feedback(self, *, subject: str, conversation_id: UUID) -> None:
        async with transaction(self.engine) as conn:
            conversation = await owned_conversation(
                conn, subject=subject, conversation_id=conversation_id
            )
            await conn.execute(
                delete(THREAD_FEEDBACK).where(
                    THREAD_FEEDBACK.c.user_id == conversation["user_id"],
                    THREAD_FEEDBACK.c.conversation_id == conversation_id,
                )
            )
