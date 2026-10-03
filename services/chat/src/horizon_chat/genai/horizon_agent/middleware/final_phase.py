"""Buffer decision calls, force required retrieval, then stream a tool-free final answer."""

from collections.abc import Awaitable, Callable
from uuid import uuid4

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from horizon_chat.genai.horizon_agent.prompts import FINAL_PROMPT, NO_EVIDENCE_RESPONSE
from horizon_chat.genai.horizon_agent.schemas import CITATION, AgentContext, attempt_context
from horizon_chat.genai.horizon_agent.tools import RAG_TOOL_NAME
from horizon_chat.ports.agent import AgentOutputError


def final_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Keep conversation context while exposing citations/evidence from this turn only."""
    latest_input = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=-1,
    )
    result: list[AnyMessage] = []
    for index, message in enumerate(messages):
        if index < latest_input and isinstance(message, ToolMessage):
            message = message.model_copy(
                update={
                    "content": "Historical tool result omitted. Search again for current evidence."
                }
            )
        elif (
            index < latest_input
            and isinstance(message, AIMessage)
            and CITATION.search(message.text)
        ):
            message = message.model_copy(update={"content": CITATION.sub("", message.text)})
        result.append(message)
    return result


class FinalAnswerPhase(AgentMiddleware[AgentState[None], AgentContext, None]):
    def __init__(self, *, final_model: BaseChatModel) -> None:
        self.final_model = final_model

    async def awrap_model_call(
        self,
        request: ModelRequest[AgentContext],
        handler: Callable[[ModelRequest[AgentContext]], Awaitable[ModelResponse[None]]],
    ) -> ModelResponse[None]:
        context = attempt_context.get()
        response = await handler(request)
        message = response.result[-1]
        if not isinstance(message, AIMessage):
            raise AgentOutputError("decision_structure")
        # A draft can reuse checkpoint citations even when the guardrail misses a
        # document follow-up. Recheck access/publication through this turn's search.
        if CITATION.search(message.text):
            context.revalidate_citations = True
        if message.tool_calls and context.searches < context.search_limit:
            return response
        needs_evidence = context.requires_search or context.revalidate_citations
        if needs_evidence and not context.searches:
            return ModelResponse(
                result=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": RAG_TOOL_NAME,
                                "args": {"query": request.runtime.context.input[:2000], "k": 5},
                                "id": str(uuid4()),
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            )
        if needs_evidence and not context.hits:
            context.scope_response = NO_EVIDENCE_RESPONSE
            return ModelResponse(result=[AIMessage(content=context.scope_response)])
        allowed_markers = ", ".join(f"[{hit.source.marker}]" for hit in context.hits.values())
        citation_rule = (
            f"\nCurrent-turn allowed citation markers: {allowed_markers}. Use only these markers."
            if allowed_markers
            else "\nThere are no current-turn sources. Do not include any citation markers."
        )
        if context.requires_search and allowed_markers:
            citation_rule += (
                " This turn requires document grounding: include at least one allowed marker."
                " If the sources do not support the requested claim, state that limitation"
                " and cite the sources you inspected, without attributing unsupported facts to them."
            )
        final = await handler(
            request.override(
                model=self.final_model,
                tools=[],
                tool_choice=None,
                messages=final_messages(request.messages),
                system_message=SystemMessage(content=FINAL_PROMPT + citation_rule),
            )
        )
        if any(isinstance(item, AIMessage) and item.tool_calls for item in final.result):
            raise AgentOutputError("final_phase_tool_call")
        return final
