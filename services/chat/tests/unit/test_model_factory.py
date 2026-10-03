"""Pinned SDK payload contracts, without contacting AWS."""

from collections.abc import Iterator
from copy import deepcopy

import pytest
from botocore.stub import ANY, Stubber
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

from horizon_chat.genai.horizon_agent.llms import AgentModels, build_models
from horizon_chat.genai.horizon_agent.schemas import AttemptContext, ScopeDecision, attempt_context
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry
from horizon_genai import BedrockConnection


def runtime_client(model: BaseChatModel) -> BedrockRuntimeClient:
    """The integration builds its own client from the factory's config."""
    assert isinstance(model, ChatBedrockConverse)
    client: BedrockRuntimeClient = model.client
    return client


@pytest.fixture
def models() -> Iterator[AgentModels]:
    provider, meters = TracerProvider(), MeterProvider()
    token = attempt_context.set(AttemptContext(physical_limit=24, rewrite_limit=2, search_limit=2))
    try:
        yield build_models(
            connection=BedrockConnection(
                region="eu-west-1",
                connect_timeout_seconds=5,
                read_timeout_seconds=30,
                aws_access_key_id=SecretStr("synthetic"),
                aws_secret_access_key=SecretStr("synthetic"),
                aws_session_token=None,
            ),
            main_model_id="global.openai.gpt-5.6-terra",
            main_reasoning_effort="low",
            utility_model_id="global.openai.gpt-5.6-luna",
            utility_reasoning_effort="none",
            max_output_tokens=512,
            telemetry=Telemetry(
                tracer=provider.get_tracer("test"),
                measurements=Measurements(meter=meters.get_meter("test")),
            ),
        )
    finally:
        attempt_context.reset(token)
        provider.shutdown()
        meters.shutdown()


def test_clients_follow_configured_timeouts_and_disable_sdk_retries(models: AgentModels) -> None:
    for model in (models.decision, models.final, models.utility):
        config = runtime_client(model).meta.config
        names = ("connect_timeout", "read_timeout", "retries")  # untyped in botocore stubs
        assert {name: getattr(config, name) for name in names} == {
            "connect_timeout": 5,
            "read_timeout": 30,
            "retries": {"total_max_attempts": 1, "mode": "legacy"},
        }


async def test_converse_reasoning_usage_and_client_tools(models: AgentModels) -> None:
    built = models
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "Answer"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
        "metrics": {"latencyMs": 1},
    }
    with Stubber(runtime_client(built.decision)) as stubber:
        stubber.add_response(
            "converse",
            deepcopy(response),
            {
                "modelId": "global.openai.gpt-5.6-terra",
                "messages": ANY,
                "system": [],
                "inferenceConfig": {"maxTokens": 512},
                "additionalModelRequestFields": {"reasoning": {"effort": "low"}},
            },
        )
        result = await built.decision.ainvoke([HumanMessage(content="Question")])
        assert result.text == "Answer"
        assert result.usage_metadata and result.usage_metadata["input_tokens"] == 7
        scoped = dict(response)
        scoped["output"] = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "scope",
                            "name": "ScopeDecision",
                            "input": {"scope": "in_scope", "requires_search": True},
                        }
                    }
                ],
            }
        }
        stubber.assert_no_pending_responses()
    with Stubber(runtime_client(built.utility)) as stubber:
        stubber.add_response(
            "converse",
            scoped,
            {
                "modelId": "global.openai.gpt-5.6-luna",
                "messages": ANY,
                "system": [],
                "inferenceConfig": {"maxTokens": 512},
                "toolConfig": ANY,
                "additionalModelRequestFields": {"reasoning": {"effort": "none"}},
            },
        )
        classification = await built.utility.with_structured_output(ScopeDecision).ainvoke(
            "Question"
        )
        assert isinstance(classification, ScopeDecision) and classification.requires_search
        stubber.assert_no_pending_responses()


async def test_real_converse_stream_parser_exposes_incremental_text_and_usage(
    models: AgentModels,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = models
    requests: list[dict[str, object]] = []

    def stream(**kwargs: object) -> dict[str, object]:
        requests.append(kwargs)
        return {
            "stream": iter(
                [
                    {"messageStart": {"role": "assistant"}},
                    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "First "}}},
                    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "second"}}},
                    {"contentBlockStop": {"contentBlockIndex": 0}},
                    {"messageStop": {"stopReason": "end_turn"}},
                    {
                        "metadata": {
                            "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
                            "metrics": {"latencyMs": 1},
                        }
                    },
                ]
            )
        }

    monkeypatch.setattr(runtime_client(built.final), "converse_stream", stream)
    chunks = [chunk async for chunk in built.final.astream("Question")]
    assert [chunk.text for chunk in chunks if chunk.text] == ["First ", "second"]
    assert (
        sum(chunk.usage_metadata["output_tokens"] for chunk in chunks if chunk.usage_metadata) == 3
    )
    assert requests[0]["additionalModelRequestFields"] == {"reasoning": {"effort": "low"}}
    assert "toolConfig" not in requests[0]
