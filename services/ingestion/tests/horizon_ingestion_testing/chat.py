"""Integration harness substitutes paid models while exercising the real compiled chat agent."""

from itertools import repeat
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.metrics import NoOpMeter
from opentelemetry.trace import NoOpTracer

from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.domain.retrieval import RetrievalPolicy
from horizon_chat.genai.horizon_agent.agent import AgentPolicy, build_agent
from horizon_chat.genai.horizon_agent.llms import AgentModels
from horizon_chat.genai.horizon_agent.runner import LangChainHorizonAgent
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG
from horizon_chat.genai.retrieval.retriever import Retriever
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry


def quiet_telemetry() -> Telemetry:
    """Chat telemetry for tests that do not observe spans or metrics."""
    return Telemetry(tracer=NoOpTracer(), measurements=Measurements(meter=NoOpMeter("test")))


class QueryEmbeddings(Embeddings):
    def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 1023

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat does not embed originals")


class ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable[Any, Any]:
        return self


def cited_agent(index: PgvectorEvidenceIndex) -> LangChainHorizonAgent:
    guardrail = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ScopeDecision",
                "args": {"scope": "in_scope", "requires_search": True},
                "id": "scope",
                "type": "tool_call",
            }
        ],
    )
    telemetry = quiet_telemetry()
    graph = build_agent(
        models=AgentModels(
            utility=ToolModel(messages=repeat(guardrail), disable_streaming=True),
            decision=ToolModel(messages=repeat(AIMessage(content="Draft")), disable_streaming=True),
            final=ToolModel(
                messages=repeat(AIMessage(content="Indexed evidence [S1].")), tags=[FINAL_TAG]
            ),
        ),
        retriever=Retriever(
            rewrite_model=GenericFakeChatModel(messages=repeat(AIMessage(content="evidence"))),
            embeddings=QueryEmbeddings(),
            index=index,
            policy=RetrievalPolicy(max_chunks=8, max_excerpt_chars=3000, max_evidence_chars=24000),
            dimensions=1024,
            embedding_model_id="amazon.titan-embed-text-v2:0",
            telemetry=telemetry,
        ),
        checkpointer=InMemorySaver(),
        telemetry=telemetry,
        policy=AgentPolicy(
            max_model_calls=5,
            max_tool_calls=2,
            retry_attempts=1,
            initial_backoff_seconds=0.001,
            max_backoff_seconds=0.001,
            summary_trigger_tokens=12000,
            summary_keep_messages=8,
        ),
    )
    return LangChainHorizonAgent(
        graph=graph,
        physical_limit=24,
        rewrite_limit=2,
        search_limit=2,
        deadline_seconds=5,
        telemetry=telemetry,
    )
