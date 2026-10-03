"""Bounded agent execution capability with no framework state in its public contract."""

from collections.abc import AsyncGenerator
from typing import Protocol

from horizon_chat.domain.agent import AgentEvent
from horizon_chat.domain.runs import Admission, CheckpointStart
from horizon_chat.ports.errors import DataIntegrityError, DependencyUnavailableError, RejectedError


class AgentUnavailableError(DependencyUnavailableError):
    """A required model or search dependency is unavailable or exhausted its retries."""


class AgentRejectedError(RejectedError):
    """The provider rejected this turn's input itself, e.g. it exceeds the context."""


class AgentIndexIntegrityError(DataIntegrityError):
    """Stored evidence violates the index contract, so citations cannot be trusted."""


class AgentBudgetError(RejectedError):
    """A physical-attempt, tool, or wall-clock budget of this turn has been exhausted."""


class AgentOutputError(RejectedError):
    """The final model output contains fabricated source markers or invalid structure."""


class AgentProtocolError(AgentOutputError):
    """A successful provider response (model, guardrail or embedding) could not be parsed."""


class HorizonAgent(Protocol):
    """Raises the errors above; each subclasses the classification base it belongs to."""

    def answer(
        self,
        *,
        subject: str,
        admission: Admission,
        start: CheckpointStart,
    ) -> AsyncGenerator[AgentEvent]: ...
