"""Technology-neutral agent events: answer text, progress and cited sources."""

from dataclasses import dataclass
from enum import StrEnum

from horizon_chat.domain.sources import Source


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerDelta:
    text: str


class ProgressPhase(StrEnum):
    """Public stages of an answer, without exposing internal model activity."""

    GUARDRAIL = "guardrail"
    RETRIEVAL = "retrieval"
    GENERATION = "generation"


PROGRESS_MESSAGES: dict[ProgressPhase, str] = {
    ProgressPhase.GUARDRAIL: "Taking a look at your question…",
    ProgressPhase.RETRIEVAL: "Looking for relevant information in the Horizon documents…",
    ProgressPhase.GENERATION: "Putting your answer together…",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentProgress:
    """A display-ready update for the current answer stage."""

    phase: ProgressPhase

    @property
    def message(self) -> str:
        return PROGRESS_MESSAGES[self.phase]


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerSources:
    sources: tuple[Source, ...]


type AgentEvent = AnswerDelta | AgentProgress | AnswerSources
