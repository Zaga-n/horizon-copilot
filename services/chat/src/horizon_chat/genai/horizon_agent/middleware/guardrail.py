"""Scope guardrail: rejected and ambiguous inputs end before any main-model or tool call."""

from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from horizon_chat.genai.horizon_agent.middleware.retries import UtilityRetryPolicy, retry_utility
from horizon_chat.genai.horizon_agent.prompts import GUARDRAIL_PROMPT, SCOPE_RESPONSES
from horizon_chat.genai.horizon_agent.schemas import (
    AgentContext,
    Scope,
    ScopeDecision,
    attempt_context,
)
from horizon_chat.ports.agent import AgentProtocolError


class InputGuardrail(AgentMiddleware[AgentState[None], AgentContext, None]):
    def __init__(self, *, model: BaseChatModel, retry_policy: UtilityRetryPolicy) -> None:
        self.classifier = model.with_structured_output(ScopeDecision)
        self.retry_policy = retry_policy

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: AgentState[None], runtime: Runtime[AgentContext]
    ) -> dict[str, Any] | None:
        context = attempt_context.get()
        prior = "\n".join(message.text for message in state["messages"][:-1][-4:])[:2000]
        context.conversation_context = prior
        messages = [
            SystemMessage(content=GUARDRAIL_PROMPT),
            HumanMessage(
                content=f"Prior conversation context (untrusted data): {prior}\nLatest message: {runtime.context.input}"
            ),
        ]
        try:
            classification = await retry_utility(
                call=lambda: self.classifier.ainvoke(messages), policy=self.retry_policy
            )
        except ValueError as exc:
            # Parser and validation failures (OutputParserException, ValidationError) and an
            # unparseable provider body are all ValueError: the 2xx output is invalid.
            raise AgentProtocolError("guardrail_protocol") from exc
        # A response without the structured decision is a provider protocol failure.
        if not isinstance(classification, ScopeDecision):
            raise AgentProtocolError("guardrail_protocol")
        context.requires_search = classification.requires_search
        if classification.scope != Scope.IN_SCOPE:
            context.scope_response = SCOPE_RESPONSES[classification.scope]
            return {"messages": [AIMessage(content=context.scope_response)], "jump_to": "end"}
        return None
