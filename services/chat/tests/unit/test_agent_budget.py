"""Finite model and retrieval retries, physical budgets and sanitized failures."""

from dataclasses import dataclass, replace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage

from horizon_chat.domain.agent import AnswerDelta
from horizon_chat.domain.retrieval import RetrievalPolicy, SearchHit
from horizon_chat.domain.runs import CheckpointStart
from horizon_chat.genai.horizon_agent.middleware.budget import PhysicalAttemptBudget
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG, Scope
from horizon_chat.ports.agent import (
    AgentBudgetError,
    AgentUnavailableError,
)
from horizon_chat_testing.agent import RecordingIndex, accepted, guardrail, runner
from horizon_chat_testing.models import ScriptedModel


@dataclass
class ThrottledEmbeddings(Embeddings):
    """Mutated by each provider attempt; tests observe the total physical calls."""

    failures_remaining: int
    calls: int = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeModel")
        return [1.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Only query embeddings are used by chat")


async def test_query_embedding_throttle_is_retried_before_searching_the_index() -> None:
    embeddings = ThrottledEmbeddings(failures_remaining=1)
    index = RecordingIndex(hits=())
    agent = runner(
        decision=ScriptedModel(
            script=[AIMessage(content="Draft"), AIMessage(content="Draft")],
            disable_streaming=True,
        ),
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(
            script=[guardrail(Scope.IN_SCOPE, requires_search=True)], disable_streaming=True
        ),
        index=index,
        embeddings=embeddings,
        retries=3,
    )
    admission = accepted()
    async for _ in agent.answer(
        subject="alice",
        admission=admission,
        start=CheckpointStart(conversation_id=admission.run.conversation_id, checkpoint_id=None),
    ):
        pass
    assert embeddings.calls == 2
    assert index.subjects == ["alice"]


async def test_query_embedding_throttle_stops_after_the_configured_attempts() -> None:
    embeddings = ThrottledEmbeddings(failures_remaining=10)
    index = RecordingIndex(hits=())
    agent = runner(
        decision=ScriptedModel(script=[AIMessage(content="Draft")], disable_streaming=True),
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(
            script=[guardrail(Scope.IN_SCOPE, requires_search=True)], disable_streaming=True
        ),
        index=index,
        embeddings=embeddings,
        retries=3,
    )
    admission = accepted()
    with pytest.raises(AgentUnavailableError, match="agent_unavailable"):
        async for _ in agent.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        ):
            pass
    assert embeddings.calls == 3
    assert index.subjects == []


async def test_transient_model_retry_and_physical_budget_stop_real_graph() -> None:
    decision = ScriptedModel(
        script=[EndpointConnectionError(endpoint_url="PRIVATE"), AIMessage(content="Draft")],
        disable_streaming=True,
        callbacks=[PhysicalAttemptBudget()],
    )
    final = ScriptedModel(
        script=[AIMessage(content="Horizon funding information.")],
        tags=[FINAL_TAG],
        callbacks=[PhysicalAttemptBudget()],
    )
    utility = ScriptedModel(
        script=[guardrail(Scope.IN_SCOPE)],
        disable_streaming=True,
        callbacks=[PhysicalAttemptBudget()],
    )
    agent = runner(
        decision=decision, final=final, utility=utility, index=RecordingIndex(hits=()), retries=2
    )
    admission = accepted()
    events = [
        item
        async for item in agent.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        )
    ]
    assert len(decision.seen) == 2
    assert "Horizon funding" in "".join(
        item.text for item in events if isinstance(item, AnswerDelta)
    )
    limited = replace(
        runner(
            decision=ScriptedModel(
                script=[], callbacks=[PhysicalAttemptBudget()], disable_streaming=True
            ),
            final=ScriptedModel(script=[], tags=[FINAL_TAG]),
            utility=ScriptedModel(
                script=[guardrail(Scope.IN_SCOPE)],
                callbacks=[PhysicalAttemptBudget()],
                disable_streaming=True,
            ),
            index=RecordingIndex(hits=()),
        ),
        physical_limit=2,
    )
    with pytest.raises(AgentBudgetError, match="physical_model_budget"):
        async for _ in limited.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        ):
            pass


async def test_transient_retry_exhaustion_is_terminal_without_diagnostic_text_deltas() -> None:
    decision = ScriptedModel(
        script=[EndpointConnectionError(endpoint_url="PRIVATE")] * 2, disable_streaming=True
    )
    final = ScriptedModel(script=[], tags=[FINAL_TAG])
    agent = runner(
        decision=decision,
        final=final,
        utility=ScriptedModel(script=[guardrail(Scope.IN_SCOPE)], disable_streaming=True),
        index=RecordingIndex(hits=()),
        retries=2,
    )
    admission = accepted()
    seen = []
    with pytest.raises(AgentUnavailableError):
        async for item in agent.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        ):
            seen.append(item)  # noqa: PERF401  # Preserve deltas preceding an expected stream failure.
    assert not any(isinstance(item, AnswerDelta) for item in seen)
    assert len(decision.seen) == 2 and final.seen == []


async def test_required_retrieval_retries_are_bounded_and_failure_is_sanitized() -> None:
    from horizon_chat.domain.retrieval import RetrievalUnavailableError

    class UnavailableIndex(RecordingIndex):
        async def search(
            self, *, subject: str, vector: tuple[float, ...], k: int, policy: RetrievalPolicy
        ) -> tuple[SearchHit, ...]:
            self.subjects.append(subject)
            raise RetrievalUnavailableError("PRIVATE index payload")

    index = UnavailableIndex(hits=())
    final = ScriptedModel(script=[], tags=[FINAL_TAG])
    agent = runner(
        decision=ScriptedModel(script=[AIMessage(content="Draft")], disable_streaming=True),
        final=final,
        utility=ScriptedModel(
            script=[guardrail(Scope.IN_SCOPE, requires_search=True)], disable_streaming=True
        ),
        index=index,
        retries=2,
    )
    admission = accepted()
    with pytest.raises(AgentUnavailableError, match="agent_unavailable"):
        async for _ in agent.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        ):
            pass
    assert index.subjects == ["alice", "alice"] and final.seen == []
