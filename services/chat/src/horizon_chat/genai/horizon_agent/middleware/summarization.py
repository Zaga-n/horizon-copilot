"""Keep the pinned summary implementation while owning its retry/backoff policy."""

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage

from horizon_chat.genai.horizon_agent.middleware.retries import UtilityRetryPolicy, retry_utility
from horizon_chat.genai.horizon_agent.schemas import AgentContext


class BoundedSummarization(SummarizationMiddleware[None, AgentContext]):
    def __init__(
        self,
        *,
        model: BaseChatModel,
        trigger_tokens: int,
        keep_messages: int,
        retry_policy: UtilityRetryPolicy,
    ) -> None:
        super().__init__(
            model=model, trigger=("tokens", trigger_tokens), keep=("messages", keep_messages)
        )
        # langchain 1.4.3 installs an unconditional three-attempt RunnableRetry here.
        # Replace it so this async service owns transient classification and configured limits.
        self._summary_model = model
        self.retry_policy = retry_policy

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        parent = super()._acreate_summary
        return await retry_utility(
            call=lambda: parent(messages_to_summarize), policy=self.retry_policy
        )
