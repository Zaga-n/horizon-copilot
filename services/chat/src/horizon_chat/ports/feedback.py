"""Answer and thread feedback owned by the conversation's subject."""

from typing import Protocol
from uuid import UUID

from horizon_chat.domain.feedback import FeedbackInput


class FeedbackStore(Protocol):
    """Raises the conversation ledger's errors (ports/conversations.py)."""

    async def put_answer_feedback(
        self, *, subject: str, message_id: UUID, request: FeedbackInput
    ) -> None: ...
    async def delete_answer_feedback(self, *, subject: str, message_id: UUID) -> None: ...
    async def put_thread_feedback(
        self, *, subject: str, conversation_id: UUID, request: FeedbackInput
    ) -> None: ...
    async def delete_thread_feedback(self, *, subject: str, conversation_id: UUID) -> None: ...
