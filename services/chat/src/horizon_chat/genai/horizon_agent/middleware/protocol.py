"""An unparseable model response becomes a named protocol failure at the model call."""

from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from horizon_chat.genai.horizon_agent.schemas import AgentContext


class ModelProtocolError(Exception):
    """Private abort: a successful model response could not be parsed; never retried."""


class ModelProtocolTranslation(AgentMiddleware[AgentState[None], AgentContext, None]):
    """Scoped to the provider call, so a ValueError elsewhere in the graph stays a defect."""

    async def awrap_model_call(
        self,
        request: ModelRequest[AgentContext],
        handler: Callable[[ModelRequest[AgentContext]], Awaitable[ModelResponse[None]]],
    ) -> ModelResponse[None]:
        try:
            return await handler(request)
        except ValueError as exc:
            # LangChain reports a 2xx provider body it cannot parse as ValueError.
            raise ModelProtocolError("model_protocol") from exc
