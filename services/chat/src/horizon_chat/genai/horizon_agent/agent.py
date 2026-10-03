"""Assemble one bounded create_agent harness without invoking or constructing providers."""

from dataclasses import dataclass
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    InputAgentState,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    OutputAgentState,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from horizon_chat.genai.horizon_agent.llms import AgentModels
from horizon_chat.genai.horizon_agent.middleware.final_phase import FinalAnswerPhase
from horizon_chat.genai.horizon_agent.middleware.guardrail import InputGuardrail
from horizon_chat.genai.horizon_agent.middleware.protocol import ModelProtocolTranslation
from horizon_chat.genai.horizon_agent.middleware.retries import (
    UtilityRetryPolicy,
    transient_model_error,
)
from horizon_chat.genai.horizon_agent.middleware.summarization import BoundedSummarization
from horizon_chat.genai.horizon_agent.prompts import SYSTEM_PROMPT
from horizon_chat.genai.horizon_agent.schemas import AgentContext
from horizon_chat.genai.horizon_agent.tools import (
    RAG_TOOL_NAME,
    SearchUnavailableError,
    build_rag_tool,
)
from horizon_chat.genai.retrieval.retriever import Retriever
from horizon_chat.observability.genai import ToolTelemetry
from horizon_chat.observability.tracing import Telemetry

AGENT_VERSION = "horizon-v2"  # bump when the graph, middleware or tool contract changes

type AgentGraph = CompiledStateGraph[
    AgentState[None], AgentContext, InputAgentState, OutputAgentState[None]
]


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentPolicy:
    """Main-loop, summary, and retry limits resolved by bootstrap."""

    max_model_calls: int
    max_tool_calls: int
    retry_attempts: int
    initial_backoff_seconds: float
    max_backoff_seconds: float
    summary_trigger_tokens: int
    summary_keep_messages: int


def build_agent(
    *,
    models: AgentModels,
    retriever: Retriever,
    checkpointer: BaseCheckpointSaver[int] | BaseCheckpointSaver[str],
    policy: AgentPolicy,
    telemetry: Telemetry,
    capture_ai_content: bool,
) -> AgentGraph:
    utility_retry = UtilityRetryPolicy(
        attempts=policy.retry_attempts,
        initial_seconds=policy.initial_backoff_seconds,
        max_seconds=policy.max_backoff_seconds,
    )
    # Middleware extends graph state with counters; its framework state type is dynamic.
    middleware: list[AgentMiddleware[Any, AgentContext, None]] = [
        InputGuardrail(model=models.utility, retry_policy=utility_retry),
        BoundedSummarization(
            model=models.utility,
            trigger_tokens=policy.summary_trigger_tokens,
            keep_messages=policy.summary_keep_messages,
            retry_policy=utility_retry,
        ),
        ModelCallLimitMiddleware(run_limit=policy.max_model_calls, exit_behavior="error"),
        ToolCallLimitMiddleware(tool_name=RAG_TOOL_NAME, run_limit=policy.max_tool_calls),
        FinalAnswerPhase(final_model=models.final),
        ModelRetryMiddleware(
            max_retries=policy.retry_attempts - 1,
            retry_on=transient_model_error,
            on_failure="error",
            initial_delay=policy.initial_backoff_seconds,
            max_delay=policy.max_backoff_seconds,
        ),
        # Inside the retry: each physical attempt is translated, and the result is not retried.
        ModelProtocolTranslation(),
        # Only an unavailable search is retried; rejected and invalid output fail the same way.
        ToolRetryMiddleware(
            max_retries=policy.retry_attempts - 1,
            retry_on=(SearchUnavailableError,),
            on_failure="error",
            initial_delay=policy.initial_backoff_seconds,
            max_delay=policy.max_backoff_seconds,
        ),
        ToolTelemetry(telemetry=telemetry, capture_ai_content=capture_ai_content),
    ]
    return create_agent(
        model=models.decision,
        tools=[build_rag_tool(retriever=retriever)],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=middleware,
        context_schema=AgentContext,
    )
