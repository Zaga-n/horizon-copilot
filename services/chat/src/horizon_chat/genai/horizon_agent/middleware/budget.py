"""Provider callbacks debit every physical model attempt, including utility calls and retries."""

from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk

from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG, attempt_context
from horizon_chat.ports.agent import AgentBudgetError


class PhysicalAttemptBudget(AsyncCallbackHandler):
    """Raises before inference; never records message content."""

    raise_error = True

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        context = attempt_context.get()
        try:
            context.debit_model(final=FINAL_TAG in (tags or []))
        except AgentBudgetError:
            context.denied_model_runs.add(run_id)
            raise

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if (
            isinstance(chunk, ChatGenerationChunk)
            and chunk.message.text
            and FINAL_TAG in (tags or [])
        ):
            attempt_context.get().final_started = True
