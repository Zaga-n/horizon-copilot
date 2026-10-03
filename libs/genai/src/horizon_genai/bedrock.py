"""Bedrock connection policy and the embedding and chat-model factories built on it."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeGuard

import boto3
from botocore.config import Config
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from horizon_genai.errors import GenAIProtocolError

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

type ReasoningEffort = Literal["none", "low", "medium", "high"]


@dataclass(frozen=True, slots=True, kw_only=True)
class BedrockConnection:
    """Region, timeouts, and optional static credentials (None: the default AWS chain)."""

    region: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None


def bedrock_config(connection: BedrockConnection) -> Config:
    # SDK retries are off: one call is one physical attempt, and callers own retries.
    return Config(
        connect_timeout=connection.connect_timeout_seconds,
        read_timeout=connection.read_timeout_seconds,
        retries={"total_max_attempts": 1},
    )


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def bedrock_runtime_client(connection: BedrockConnection) -> "BedrockRuntimeClient":
    """A new runtime client the caller owns and closes; construction blocks, so use a thread."""
    return boto3.client(
        "bedrock-runtime",
        region_name=connection.region,
        aws_access_key_id=_secret(connection.aws_access_key_id),
        aws_secret_access_key=_secret(connection.aws_secret_access_key),
        aws_session_token=_secret(connection.aws_session_token),
        config=bedrock_config(connection),
    )


def build_bedrock_embeddings(
    *, client: "BedrockRuntimeClient", model_id: str, dimensions: int
) -> Embeddings:
    """Titan embeddings over a borrowed client; normalized so query and document vectors match."""
    return BedrockEmbeddings(
        client=client, model_id=model_id, dimensions=dimensions, normalize=True
    )


def _is_finite_number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def checked_vector(values: object, *, dimensions: int) -> tuple[float, ...]:
    """The vector of a successful response, or a protocol error when it is unusable.

    `values` comes from an untrusted 2xx body, so its shape and element types are checked.
    """
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise GenAIProtocolError("embedding_dimensions_or_values")
    vector = tuple(value for value in values if _is_finite_number(value))
    if len(vector) != len(values) or len(vector) != dimensions:
        raise GenAIProtocolError("embedding_dimensions_or_values")
    return vector


def build_bedrock_chat_model(
    *,
    connection: BedrockConnection,
    model_id: str,
    reasoning_effort: ReasoningEffort,
    max_tokens: int,
    streaming: bool,
    callbacks: Sequence[BaseCallbackHandler] = (),
    tags: Sequence[str] = (),
) -> BaseChatModel:
    """A Converse chat model whose internal client follows the shared connection policy."""
    return ChatBedrockConverse(
        model_id=model_id,
        region_name=connection.region,
        config=bedrock_config(connection),
        aws_access_key_id=connection.aws_access_key_id,
        aws_secret_access_key=connection.aws_secret_access_key,
        aws_session_token=connection.aws_session_token,
        max_tokens=max_tokens,
        disable_streaming=not streaming,
        reasoning_effort=reasoning_effort,
        callbacks=list(callbacks),
        tags=list(tags),
    )
