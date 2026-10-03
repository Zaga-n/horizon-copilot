"""Decision, final, and utility chat models; every tuning value comes from settings."""

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from horizon_chat.genai.horizon_agent.middleware.budget import PhysicalAttemptBudget
from horizon_chat.genai.horizon_agent.prompts import PROMPT_VERSION
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG
from horizon_chat.observability.genai import ModelTelemetry
from horizon_chat.observability.tracing import Telemetry
from horizon_genai import BedrockConnection, ReasoningEffort, build_bedrock_chat_model


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentModels:
    """Decision and utility inference is buffered; only the final model streams visible text."""

    decision: BaseChatModel
    final: BaseChatModel
    utility: BaseChatModel


def build_chat_model(
    *,
    connection: BedrockConnection,
    model_id: str,
    reasoning_effort: ReasoningEffort,
    max_output_tokens: int,
    streaming: bool,
    telemetry: Telemetry,
) -> BaseChatModel:
    return build_bedrock_chat_model(
        connection=connection,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        max_tokens=max_output_tokens,
        streaming=streaming,
        callbacks=(
            PhysicalAttemptBudget(),
            ModelTelemetry(telemetry=telemetry, model_id=model_id, prompt_version=PROMPT_VERSION),
        ),
        tags=(FINAL_TAG,) if streaming else (),
    )


def build_models(
    *,
    connection: BedrockConnection,
    main_model_id: str,
    main_reasoning_effort: ReasoningEffort,
    utility_model_id: str,
    utility_reasoning_effort: ReasoningEffort,
    max_output_tokens: int,
    telemetry: Telemetry,
) -> AgentModels:
    def main(*, streaming: bool) -> BaseChatModel:
        return build_chat_model(
            connection=connection,
            model_id=main_model_id,
            reasoning_effort=main_reasoning_effort,
            max_output_tokens=max_output_tokens,
            streaming=streaming,
            telemetry=telemetry,
        )

    return AgentModels(
        decision=main(streaming=False),
        final=main(streaming=True),
        utility=build_chat_model(
            connection=connection,
            model_id=utility_model_id,
            reasoning_effort=utility_reasoning_effort,
            max_output_tokens=max_output_tokens,
            streaming=False,
            telemetry=telemetry,
        ),
    )
