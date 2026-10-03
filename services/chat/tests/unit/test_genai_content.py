"""Opt-in capture preserves Bedrock content while metrics and capture-off paths stay clean."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from horizon_chat.observability.genai import MAX_PARTIAL_OUTPUT_CHARS, ModelTelemetry
from horizon_chat.observability.genai_content import (
    INPUT_CAPTURE_MODE,
    INPUT_MESSAGES,
    OBSERVATION_INPUT,
    OBSERVATION_OUTPUT,
    OUTPUT_CAPTURE_MODE,
    OUTPUT_MESSAGES,
    SYSTEM_INSTRUCTIONS,
    serialize_input,
    serialize_output,
)
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry

CANARY = "genai-content-canary"


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureHarness:
    telemetry: Telemetry
    exporter: InMemorySpanExporter


@pytest.fixture
def harness() -> Iterator[CaptureHarness]:
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    meters = MeterProvider()
    try:
        yield CaptureHarness(
            telemetry=Telemetry(
                tracer=traces.get_tracer("test"),
                measurements=Measurements(meter=meters.get_meter("test")),
            ),
            exporter=exporter,
        )
    finally:
        traces.shutdown()
        meters.shutdown()


@pytest.mark.parametrize("capture", [False, True])
async def test_capture_switch_controls_system_history_and_structured_output(
    harness: CaptureHarness, capture: bool
) -> None:
    callback = ModelTelemetry(
        telemetry=harness.telemetry,
        model_id="bedrock-canary",
        prompt_version="v1",
        capture_ai_content=capture,
    )
    run_id = uuid4()
    await callback.on_chat_model_start(
        {},
        [[SystemMessage(content="system " + CANARY), HumanMessage(content=CANARY)]],
        run_id=run_id,
    )
    await callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content=json.dumps({"answer": CANARY}),
                            response_metadata={"stopReason": "end_turn"},
                        )
                    )
                ]
            ]
        ),
        run_id=run_id,
    )
    spans = harness.exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert (CANARY in str(attrs)) == capture
    if capture:
        assert json.loads(str(attrs[SYSTEM_INSTRUCTIONS])) == [
            {"type": "text", "content": "system " + CANARY}
        ]
        assert json.loads(str(attrs[INPUT_MESSAGES]))[0]["role"] == "user"
        assert json.loads(str(attrs[OBSERVATION_INPUT]))[0]["role"] == "system"
        assert json.loads(str(attrs[OUTPUT_MESSAGES]))[0]["finish_reason"] == "end_turn"
        assert json.loads(str(attrs[OBSERVATION_OUTPUT])) == {"answer": CANARY}


def test_serializer_keeps_tool_history_and_signed_provider_reasoning() -> None:
    input_capture = serialize_input(
        [
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search",
                            "args": {"query": CANARY},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content=CANARY, tool_call_id="call-1"),
            ]
        ]
    )
    history = json.loads(input_capture.messages)
    assert history[0]["parts"][0]["arguments"] == {"query": CANARY}
    assert history[1]["parts"][0] == {
        "type": "tool_call_response",
        "id": "call-1",
        "response": CANARY,
    }
    reasoning = {
        "type": "reasoning_content",
        "reasoning_content": {"type": "text", "text": CANARY, "signature": "signed-canary"},
    }
    output_capture = serialize_output(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content=[reasoning, {"type": "text", "text": "answer"}],
                            response_metadata={"stopReason": "end_turn"},
                        )
                    )
                ]
            ]
        )
    )
    canonical = json.loads(output_capture.messages)
    assert canonical[0]["parts"][0] == reasoning
    assert json.loads(output_capture.observation) == canonical


async def test_batch_capture_is_marked_and_failed_stream_output_is_bounded(
    harness: CaptureHarness,
) -> None:
    callback = ModelTelemetry(
        telemetry=harness.telemetry,
        model_id="bedrock-canary",
        prompt_version="v1",
        capture_ai_content=True,
    )
    run_id = uuid4()
    await callback.on_chat_model_start(
        {},
        [[HumanMessage(content=CANARY)], [HumanMessage(content="other-conversation")]],
        run_id=run_id,
    )
    await callback.on_llm_new_token("x" * (MAX_PARTIAL_OUTPUT_CHARS + 1), run_id=run_id)
    await callback.on_llm_error(RuntimeError("private error"), run_id=run_id)
    attrs = dict(harness.exporter.get_finished_spans()[0].attributes or {})
    assert attrs[INPUT_CAPTURE_MODE] == "truncated"
    assert "other-conversation" not in str(attrs[INPUT_MESSAGES])
    assert attrs[OUTPUT_CAPTURE_MODE] == "truncated"
    assert len(json.loads(str(attrs[OBSERVATION_OUTPUT]))) == MAX_PARTIAL_OUTPUT_CHARS
    assert "private error" not in str(attrs)
