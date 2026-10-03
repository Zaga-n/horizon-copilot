"""Conversations, their message history and the availability rule for owned reads."""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, field_validator

from horizon_chat.domain.feedback import AnswerFeedback, ThreadFeedback
from horizon_chat.domain.runs import AttemptStatus, MessageRole
from horizon_chat.domain.sources import Source
from horizon_chat.domain.values import StrictModel

MAX_PAGE_SIZE = 100
MAX_TITLE_CHARS = 72


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    PURGING = "purging"


class ConversationNotFoundError(Exception):
    """The resource is missing, belongs to another owner, or is purging."""


class Conversation(StrictModel):
    id: UUID
    status: ConversationStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_activity_at: AwareDatetime
    feedback: ThreadFeedback | None = None
    title: str = "New conversation"

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: str | None) -> str:
        title = " ".join((value or "").split())
        if not title:
            return "New conversation"
        if len(title) > MAX_TITLE_CHARS:
            return title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
        return title


class Message(StrictModel):
    id: UUID
    conversation_id: UUID
    turn_id: UUID
    attempt_number: int
    role: MessageRole
    content: str
    status: AttemptStatus
    sources: tuple[Source, ...]
    message_order: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    run_id: UUID | None = None
    trace_id: str | None = None
    feedback: AnswerFeedback | None = None


class HistoryPage(StrictModel):
    messages: tuple[Message, ...]
    next_cursor: int | None


class ConversationPage(StrictModel):
    conversations: tuple[Conversation, ...]
    next_cursor: UUID | None


def ensure_available(*, status: ConversationStatus) -> None:
    if status != ConversationStatus.ACTIVE:
        raise ConversationNotFoundError()
