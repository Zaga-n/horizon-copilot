"""Stream only tagged final-generation text and validate citation metadata before completion."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from langchain.agents.middleware import InputAgentState
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.schema import StreamEvent
from langgraph.graph import MessagesState

from horizon_chat.domain.agent import (
    AgentEvent,
    AgentProgress,
    AnswerDelta,
    AnswerSources,
    ProgressPhase,
)
from horizon_chat.domain.runs import Admission, CheckpointStart
from horizon_chat.domain.sources import Source
from horizon_chat.genai.horizon_agent.agent import AgentGraph
from horizon_chat.genai.horizon_agent.middleware.protocol import ModelProtocolError
from horizon_chat.genai.horizon_agent.schemas import (
    CITATION,
    FINAL_TAG,
    AgentContext,
    AttemptContext,
    attempt_context,
)
from horizon_chat.genai.horizon_agent.tools import (
    RAG_TOOL_NAME,
    SearchIntegrityError,
    SearchInvalidOutputError,
    SearchRejectedError,
    SearchUnavailableError,
)
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.agent import (
    AgentBudgetError,
    AgentIndexIntegrityError,
    AgentOutputError,
    AgentProtocolError,
    AgentRejectedError,
    AgentUnavailableError,
)
from horizon_genai import GenAIProtocolError, GenAIProviderRejectedError, classify_bedrock_error

TRANSLATED = (
    TimeoutError,
    ModelCallLimitExceededError,
    BotoCoreError,
    ClientError,
    SearchUnavailableError,
    SearchRejectedError,
    SearchInvalidOutputError,
    SearchIntegrityError,
    ModelProtocolError,
)


def agent_error(exc: Exception) -> Exception:
    """Translate a framework, provider or tool failure into the agent port's outcome once."""
    if isinstance(exc, TimeoutError | ModelCallLimitExceededError):
        return AgentBudgetError("agent_budget")
    if isinstance(exc, SearchIntegrityError):
        return AgentIndexIntegrityError("index_integrity")
    if isinstance(exc, SearchRejectedError):
        return AgentRejectedError("embedding_rejected")
    if isinstance(exc, SearchInvalidOutputError | ModelProtocolError):
        return AgentProtocolError("provider_protocol")
    provider_error = classify_bedrock_error(exc)
    if isinstance(provider_error, GenAIProviderRejectedError):
        return AgentRejectedError("provider_rejected")
    if isinstance(provider_error, GenAIProtocolError):
        return AgentProtocolError("provider_protocol")
    return AgentUnavailableError("agent_unavailable")


def progress_phase(event: StreamEvent) -> ProgressPhase | None:
    """Expose only RAG searches and final answer generation as graph progress."""
    if event["event"] == "on_tool_start" and event["name"] == RAG_TOOL_NAME:
        return ProgressPhase.RETRIEVAL
    if event["event"] == "on_chat_model_start" and FINAL_TAG in event.get("tags", []):
        return ProgressPhase.GENERATION
    return None


def attempt_input(admission: Admission) -> MessagesState:
    return {
        "messages": [HumanMessage(content=admission.input, id=str(admission.run.user_message_id))]
    }


def attempt_config(start: CheckpointStart) -> RunnableConfig:
    configurable: dict[str, str] = {"thread_id": str(start.conversation_id)}
    if start.checkpoint_id is not None:
        configurable["checkpoint_id"] = start.checkpoint_id
    return {"configurable": configurable}


def validated_sources(*, answer: str, context: AttemptContext) -> tuple[Source, ...]:
    allowed = {hit.source.marker: hit.source for hit in context.hits.values()}
    markers = tuple(dict.fromkeys(CITATION.findall(answer)))
    if any(marker not in allowed for marker in markers):
        raise AgentOutputError("invalid_citations")
    if context.requires_search and allowed and not markers:
        raise AgentOutputError("missing_citations")
    return tuple(allowed[marker] for marker in markers)


@dataclass(frozen=True, slots=True, kw_only=True)
class LangChainHorizonAgent:
    """Contextvars isolate concurrent conversations; checkpoints remain owner/lease-gated by actions."""

    graph: AgentGraph
    physical_limit: int
    rewrite_limit: int
    search_limit: int
    deadline_seconds: float
    telemetry: Telemetry

    async def answer(
        self,
        *,
        subject: str,
        admission: Admission,
        start: CheckpointStart,
    ) -> AsyncGenerator[AgentEvent]:
        context = AttemptContext(
            physical_limit=self.physical_limit,
            rewrite_limit=self.rewrite_limit,
            search_limit=self.search_limit,
        )
        token = attempt_context.set(context)
        chunks: list[str] = []
        last_phase = ProgressPhase.GUARDRAIL
        graph_input: InputAgentState = {"messages": list(attempt_input(admission)["messages"])}
        try:
            yield AgentProgress(phase=ProgressPhase.GUARDRAIL)
            async with asyncio.timeout(self.deadline_seconds):
                async for event in self.graph.astream_events(
                    graph_input,
                    attempt_config(start),
                    context=AgentContext(subject=subject, input=admission.input),
                    version="v2",
                ):
                    phase = progress_phase(event)
                    if phase is not None and phase != last_phase:
                        last_phase = phase
                        yield AgentProgress(phase=phase)
                    if event["event"] == "on_chat_model_stream" and FINAL_TAG in event.get(
                        "tags", []
                    ):
                        chunk = event["data"].get("chunk")
                        if isinstance(chunk, AIMessageChunk) and chunk.text:
                            context.final_started = True
                            chunks.append(chunk.text)
                            yield AnswerDelta(text=chunk.text)
                if context.scope_response is not None:
                    chunks.append(context.scope_response)
                    yield AnswerDelta(text=context.scope_response)
                if not chunks:
                    raise AgentOutputError("empty_final_answer")
                yield AnswerSources(
                    sources=validated_sources(answer="".join(chunks), context=context)
                )
        except TRANSLATED as exc:
            raise agent_error(exc) from exc
        finally:
            self._measure(context)
            attempt_context.reset(token)

    def _measure(self, context: AttemptContext) -> None:
        self.telemetry.measurements.model_calls.record(context.physical_calls)
        self.telemetry.measurements.tool_calls.record(context.searches)
