"""One physical-model observation per callback run, with usage and first-chunk timing."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, GenerationChunk, LLMResult
from langgraph.types import Command
from opentelemetry.trace import Span

from horizon_chat.genai.horizon_agent.schemas import attempt_context
from horizon_chat.observability.tracing import Telemetry, mark_error


@dataclass(slots=True, kw_only=True)
class ModelObservation:
    """The callback marks first-chunk timing once for this physical attempt."""

    span: Span
    started: float
    first_chunk: bool = False


class ModelTelemetry(AsyncCallbackHandler):
    """No message/token content capture and no per-token spans."""

    def __init__(self, *, telemetry: Telemetry, model_id: str, prompt_version: str) -> None:
        self.telemetry = telemetry
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.observations: dict[UUID, ModelObservation] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        context = attempt_context.get(None)
        if context is not None and run_id in context.denied_model_runs:
            return
        self.observations[run_id] = ModelObservation(
            started=perf_counter(),
            span=self.telemetry.tracer.start_span(
                "gen_ai.chat",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "aws.bedrock",
                    "gen_ai.request.model": self.model_id,
                    "app.prompt.version": self.prompt_version,
                    "app.cost.available": False,
                },
            ),
        )

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        run_id: UUID,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        **kwargs: Any,
    ) -> None:
        if not token or (isinstance(chunk, ChatGenerationChunk) and not chunk.message.text):
            return
        observation = self.observations.get(run_id)
        if observation is not None and not observation.first_chunk:
            observation.first_chunk = True
            self.telemetry.measurements.model_first.record(
                perf_counter() - observation.started, {"gen_ai.request.model": self.model_id}
            )

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        observation = self.observations.pop(run_id, None)
        if observation is None:
            return
        observation.span.set_attribute("app.usage.available", value=False)
        for generations in response.generations[:1]:
            for generation in generations[:1]:
                if isinstance(generation, ChatGeneration) and isinstance(
                    generation.message, AIMessage
                ):
                    usage = generation.message.usage_metadata
                    if usage is not None:
                        observation.span.set_attribute("app.usage.available", value=True)
                        for kind, value in (
                            ("input", usage["input_tokens"]),
                            ("output", usage["output_tokens"]),
                        ):
                            observation.span.set_attribute(f"gen_ai.usage.{kind}_tokens", value)
                            self.telemetry.measurements.tokens.record(
                                value,
                                {"gen_ai.request.model": self.model_id, "gen_ai.token.type": kind},
                            )
        self._close(observation, outcome="ok")

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        observation = self.observations.pop(run_id, None)
        if observation is not None:
            mark_error(observation.span, error)
            self._close(observation, outcome="error")

    def _close(self, observation: ModelObservation, *, outcome: str) -> None:
        self.telemetry.measurements.model_duration.record(
            perf_counter() - observation.started,
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "aws.bedrock",
                "gen_ai.request.model": self.model_id,
                "outcome": outcome,
            },
        )
        observation.span.end()


class ToolTelemetry(AgentMiddleware[AgentState[None], Any, None]):
    """One span for each physical tool attempt; never record its arguments or output."""

    def __init__(self, *, telemetry: Telemetry) -> None:
        self.telemetry = telemetry

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        with self.telemetry.work("tool") as span:
            span.set_attribute("gen_ai.tool.name", request.tool_call["name"])
            return await handler(request)
