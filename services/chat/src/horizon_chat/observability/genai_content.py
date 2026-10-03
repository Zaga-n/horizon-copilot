"""Portable GenAI content and lossless Langfuse presentation for Bedrock Converse callbacks."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
OBSERVATION_INPUT = "app.gen_ai.observation.input"
OBSERVATION_OUTPUT = "app.gen_ai.observation.output"
INPUT_BATCH_SIZE = "app.gen_ai.input.batch_size"
INPUT_CAPTURE_MODE = "app.gen_ai.input.capture_mode"
OUTPUT_CAPTURE_MODE = "app.gen_ai.output.capture_mode"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
ROLE_BY_TYPE = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}


@dataclass(frozen=True, slots=True, kw_only=True)
class InputCapture:
    """One conversation, with system instructions separated as Bedrock sends them."""

    system_instructions: str | None
    messages: str
    observation: str
    batch_size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputCapture:
    """Canonical output choices and a lossless backend display projection."""

    messages: str
    observation: str


def _part(block: Mapping[str, Any]) -> dict[str, Any]:
    """Translate normalized framework blocks while preserving provider-specific fields."""
    if block.get("type") == "non_standard" and isinstance(block.get("value"), dict):
        return _part(block["value"])
    part = dict(block)
    kind = part.get("type")
    if kind == "text":
        part["content"] = part.pop("text", part.get("content", ""))
    elif kind == "reasoning":
        part["content"] = part.pop("reasoning", part.get("content", ""))
    elif kind == "tool_call":
        part["arguments"] = part.pop("args", part.get("arguments", {}))
        if not part.get("name"):
            part["type"] = "unknown_tool_call"
    return part


def _message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "parts": [
                {
                    "type": "tool_call_response",
                    "id": message.tool_call_id,
                    "response": message.content,
                }
            ],
        }
    return {
        "role": ROLE_BY_TYPE.get(message.type, message.type),
        "parts": [_part(block) for block in message.content_blocks],
    }


def _text_message(message: dict[str, Any]) -> dict[str, str] | None:
    """Return a text projection only when no meaningful content would be lost."""
    parts = [part for part in message["parts"] if part != {"type": "reasoning", "content": ""}]
    if len(parts) != 1 or parts[0].get("type") != "text":
        return None
    content = parts[0].get("content")
    if not isinstance(content, str):
        return None
    return {"role": message["role"], "content": content}


def serialize_input(messages: list[list[BaseMessage]]) -> InputCapture:
    conversation = [_message(message) for message in messages[0]] if messages else []
    system_parts = [
        part for message in conversation if message["role"] == "system" for part in message["parts"]
    ]
    history = [message for message in conversation if message["role"] != "system"]
    text = [_text_message(message) for message in conversation]
    presentation = text if all(message is not None for message in text) else conversation
    return InputCapture(
        system_instructions=json.dumps(system_parts, default=str) if system_parts else None,
        messages=json.dumps(history, default=str),
        observation=json.dumps(presentation, default=str),
        batch_size=len(messages),
    )


def _finish_reason(generation: ChatGeneration) -> str:
    for source in (generation.message.response_metadata, generation.generation_info or {}):
        for key in ("finish_reason", "finishReason", "stop_reason", "stopReason"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _output_presentation(messages: list[dict[str, Any]]) -> object:
    text = [_text_message(message) for message in messages]
    if not all(message is not None for message in text):
        return messages
    if len(text) != 1 or text[0] is None:
        return text
    content = text[0]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def serialize_output(response: LLMResult) -> OutputCapture:
    messages = []
    for generation in response.generations[0] if response.generations else []:
        if isinstance(generation, ChatGeneration):
            message = _message(generation.message)
            message["finish_reason"] = _finish_reason(generation)
        else:
            message = {
                "role": "assistant",
                "parts": [{"type": "text", "content": generation.text}],
                "finish_reason": "unknown",
            }
        messages.append(message)
    return OutputCapture(
        messages=json.dumps(messages, default=str),
        observation=json.dumps(_output_presentation(messages), default=str),
    )
