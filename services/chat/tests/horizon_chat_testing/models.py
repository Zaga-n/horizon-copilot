"""Scripted tool-capable models for deterministic application protocol checks."""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, SkipValidation


class ScriptedModel(BaseChatModel):
    """Records requests and fails on unscripted calls; final generation genuinely yields chunks."""

    script: SkipValidation[list[AIMessage | Exception]]
    stream_gate: SkipValidation[asyncio.Event | None] = None
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[list[str]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-horizon"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        self.bound_tools.append(
            [convert_to_openai_tool(item)["function"]["name"] for item in tools]
        )
        return self

    def next_reply(self, messages: list[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        if len(self.seen) > len(self.script):
            raise AssertionError("unscripted model invocation")
        response = self.script[len(self.seen) - 1]
        if isinstance(response, Exception):
            raise response
        return response.model_copy(update={"id": f"script-{len(self.seen)}"})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.next_reply(messages))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        reply = self.next_reply(messages)
        # Deliberately split text; tests assert reconstructed output, never chunk boundaries.
        text = reply.text
        midpoint = max(1, len(text) // 2)
        yield ChatGenerationChunk(message=AIMessageChunk(content=text[:midpoint]))
        if self.stream_gate is not None:
            await self.stream_gate.wait()
        yield ChatGenerationChunk(message=AIMessageChunk(content=text[midpoint:]))
