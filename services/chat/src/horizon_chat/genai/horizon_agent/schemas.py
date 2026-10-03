"""Agent context, per-attempt accumulators, and the guardrail's structured output."""

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from horizon_chat.domain.retrieval import SearchHit
from horizon_chat.domain.values import StrictModel
from horizon_chat.ports.agent import AgentBudgetError

# Tag on the streaming final model; callbacks and the runner select visible text by it.
FINAL_TAG = "horizon-final"
# Shared by decision-phase retrieval enforcement and final citation validation.
CITATION = re.compile(r"\[(S[^\]\n]*|\d+)\]")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentContext:
    """Trusted per-invocation values passed as the agent's `context_schema`."""

    subject: str
    input: str


@dataclass(slots=True, kw_only=True)
class AttemptContext:
    """Per-attempt budget and evidence accumulators.

    Bound in a ContextVar because provider callbacks (the physical-attempt budget,
    model telemetry) receive no LangGraph runtime, and the runner reads the
    collected evidence after the graph finishes.
    """

    physical_limit: int
    rewrite_limit: int
    search_limit: int
    conversation_context: str = ""
    physical_calls: int = 0
    searches: int = 0
    rewrites: int = 0
    requires_search: bool = False
    revalidate_citations: bool = False
    scope_response: str | None = None
    final_started: bool = False
    denied_model_runs: set[UUID] = field(default_factory=set)
    hits: dict[str, SearchHit] = field(default_factory=dict)

    def debit_model(self, *, final: bool) -> None:
        ceiling = self.physical_limit if final else self.physical_limit - 1
        if self.physical_calls >= ceiling:
            raise AgentBudgetError("physical_model_budget")
        self.physical_calls += 1


attempt_context: ContextVar[AttemptContext] = ContextVar("horizon_attempt_context")


class Scope(StrEnum):
    IN_SCOPE = "in_scope"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"
    INJECTION = "injection"


class ScopeDecision(StrictModel):
    scope: Scope
    requires_search: bool
