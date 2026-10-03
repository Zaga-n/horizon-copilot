"""One physical-model observation per callback run, with usage and first-chunk timing."""

import json
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
from horizon_chat.observability.genai_content import (
    INPUT_BATCH_SIZE,
    INPUT_CAPTURE_MODE,
    INPUT_MESSAGES,
    OBSERVATION_INPUT,
    OBSERVATION_OUTPUT,
    OUTPUT_CAPTURE_MODE,
    OUTPUT_MESSAGES,
    SYSTEM_INSTRUCTIONS,
    TOOL_ARGUMENTS,
    TOOL_RESULT,
    serialize_input,
    serialize_output,
)
from horizon_chat.observability.tracing import Telemetry, mark_error

MAX_PARTIAL_OUTPUT_CHARS = 32_768


@dataclass(slots=True, kw_only=True)
class ModelObservation:
    """The callback marks first-chunk timing once for this physical attempt."""

    span: Span
    started: float
    first_chunk: bool = False
    partial_text: str = ""
    partial_truncated: bool = False


class ModelTelemetry(AsyncCallbackHandler):
    """Physical model calls with opt-in content; no per-token spans."""

    def __init__(
        self, *, telemetry: Telemetry, model_id: str, prompt_version: str, capture_ai_content: bool
    ) -> None:
        self.telemetry = telemetry
        self.model_id = model_id
        self.prompt_version = prompt_version
        self.capture_ai_content = capture_ai_content
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
        if self.capture_ai_content:
            self._capture_input(self.observations[run_id].span, messages)

    def _capture_input(self, span: Span, messages: list[list[BaseMessage]]) -> None:
        capture = serialize_input(messages)
        span.set_attribute(INPUT_MESSAGES, capture.messages)
        span.set_attribute(OBSERVATION_INPUT, capture.observation)
        span.set_attribute(INPUT_BATCH_SIZE, capture.batch_size)
        if capture.system_instructions is not None:
            span.set_attribute(SYSTEM_INSTRUCTIONS, capture.system_instructions)
        if capture.batch_size > 1:
            span.set_attribute(INPUT_CAPTURE_MODE, "truncated")

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
        if observation is not None and self.capture_ai_content:
            text = chunk.message.text if isinstance(chunk, ChatGenerationChunk) else token
            if isinstance(text, str):
                remaining = MAX_PARTIAL_OUTPUT_CHARS - len(observation.partial_text)
                observation.partial_text += text[:remaining]
                observation.partial_truncated |= len(text) > remaining
        if observation is not None and not observation.first_chunk:
            observation.first_chunk = True
            self.telemetry.measurements.model_first.record(
                perf_counter() - observation.started, {"gen_ai.request.model": self.model_id}
            )

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        observation = self.observations.pop(run_id, None)
        if observation is None:
            return
        if self.capture_ai_content:
            capture = serialize_output(response)
            observation.span.set_attribute(OUTPUT_MESSAGES, capture.messages)
            observation.span.set_attribute(OBSERVATION_OUTPUT, capture.observation)
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
            if self.capture_ai_content and observation.partial_text:
                self._capture_partial(observation)
            mark_error(observation.span, error)
            self._close(observation, outcome="error")

    def _capture_partial(self, observation: ModelObservation) -> None:
        capture = serialize_output(
            LLMResult(
                generations=[[ChatGeneration(message=AIMessage(content=observation.partial_text))]]
            )
        )
        observation.span.set_attribute(OUTPUT_MESSAGES, capture.messages)
        observation.span.set_attribute(OBSERVATION_OUTPUT, capture.observation)
        observation.span.set_attribute(
            OUTPUT_CAPTURE_MODE, "truncated" if observation.partial_truncated else "partial"
        )

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
    """One span for each physical tool attempt, with opt-in arguments and results."""

    def __init__(self, *, telemetry: Telemetry, capture_ai_content: bool) -> None:
        self.telemetry = telemetry
        self.capture_ai_content = capture_ai_content

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        with self.telemetry.work("tool") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", request.tool_call["name"])
            if self.capture_ai_content:
                arguments = json.dumps(request.tool_call["args"], default=str)
                span.set_attribute(TOOL_ARGUMENTS, arguments)
                span.set_attribute(OBSERVATION_INPUT, arguments)
            result = await handler(request)
            if self.capture_ai_content and isinstance(result, ToolMessage):
                output = json.dumps(result.content, default=str)
                span.set_attribute(TOOL_RESULT, output)
                span.set_attribute(OBSERVATION_OUTPUT, output)
            return result
