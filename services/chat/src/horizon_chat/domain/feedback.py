"""Answer and thread feedback values and the rule for which answers accept ratings."""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from horizon_chat.domain.runs import AttemptStatus, MessageRole
from horizon_chat.domain.values import StorableText, StrictModel

MAX_COMMENT_CHARS = 4000


class Rating(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"


class FeedbackTargetError(Exception):
    """Answer feedback requires a completed assistant attempt."""


class AnswerFeedback(StrictModel):
    rating: Rating
    comment: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ThreadFeedback(StrictModel):
    """Thread feedback captures its own context and permits text-only input."""

    rating: Rating | None
    comment: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    context_run_id: UUID | None
    context_message_order: int


class FeedbackInput(StrictModel):
    rating: Rating | None = None
    comment: Annotated[StorableText, Field(max_length=MAX_COMMENT_CHARS)] | None = None

    @field_validator("comment")
    @classmethod
    def trim_comment(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @model_validator(mode="after")
    def require_value(self) -> Self:
        if self.rating is None and self.comment is None:
            raise ValueError("rating or nonempty comment is required")
        return self


def answer_feedback(
    *, status: AttemptStatus, role: MessageRole, request: FeedbackInput
) -> FeedbackInput:
    if status != AttemptStatus.COMPLETED or role != MessageRole.ASSISTANT:
        raise FeedbackTargetError()
    if request.rating is None:
        raise FeedbackTargetError()
    return FeedbackInput(
        rating=request.rating, comment=request.comment if request.rating == Rating.DISLIKE else None
    )
