"""Shared Bedrock connection policy, model factories and provider error classification."""

from horizon_genai.bedrock import (
    BedrockConnection,
    ReasoningEffort,
    bedrock_config,
    bedrock_runtime_client,
    build_bedrock_chat_model,
    build_bedrock_embeddings,
    checked_vector,
)
from horizon_genai.errors import (
    GenAIError,
    GenAIProtocolError,
    GenAIProviderRejectedError,
    GenAIProviderUnavailableError,
    classify_bedrock_error,
)
from horizon_genai.usage import observe_titan_usage

__all__ = [
    "BedrockConnection",
    "GenAIError",
    "GenAIProtocolError",
    "GenAIProviderRejectedError",
    "GenAIProviderUnavailableError",
    "ReasoningEffort",
    "bedrock_config",
    "bedrock_runtime_client",
    "build_bedrock_chat_model",
    "build_bedrock_embeddings",
    "checked_vector",
    "classify_bedrock_error",
    "observe_titan_usage",
]
