"""Typed SSE inputs and event envelopes with stable run attribution."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from horizon_chat.domain.agent import ProgressPhase
from horizon_chat.domain.runs import FailureCategory
from horizon_chat.domain.sources import Source
from horizon_chat.domain.values import StorableText, StrictModel


class TurnInput(StrictModel):
    content: Annotated[StorableText, Field(min_length=1, max_length=16000)]


class RetryInput(StrictModel):
    expected_run_id: UUID


class ProgressData(StrictModel):
    phase: ProgressPhase
    message: str


class DeltaData(StrictModel):
    text: str


class SourcesData(StrictModel):
    sources: tuple[Source, ...]


class EmptyData(StrictModel):
    """Started/completed events need only the envelope's stable attribution."""


class FailedData(StrictModel):
    failure_category: FailureCategory
    persistence_pending: bool
    retry_available: bool
    text: str


type EventType = Literal["started", "progress", "delta", "sources", "completed", "failed"]
DATA_TYPES: dict[EventType, type[StrictModel]] = {
    "started": EmptyData,
    "progress": ProgressData,
    "delta": DeltaData,
    "sources": SourcesData,
    "completed": EmptyData,
    "failed": FailedData,
}


class StreamEvent(StrictModel):
    event_id: str
    event: EventType = Field(serialization_alias="type")
    conversation_id: UUID
    turn_id: UUID
    run_id: UUID
    assistant_message_id: UUID
    attempt_number: int
    trace_id: str
    sequence: int
    data: ProgressData | DeltaData | SourcesData | EmptyData | FailedData

    @model_validator(mode="after")
    def match_event_data(self) -> Self:
        if type(self.data) is not DATA_TYPES[self.event]:
            raise ValueError("event data does not match type")
        return self
