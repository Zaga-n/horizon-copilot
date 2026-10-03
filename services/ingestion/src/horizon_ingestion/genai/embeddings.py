"""Titan document embeddings: one physical attempt, translated once into the embedding port."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.embeddings import Embeddings

from horizon_genai import (
    GenAIError,
    GenAIProtocolError,
    GenAIProviderRejectedError,
    GenAIProviderUnavailableError,
    build_bedrock_embeddings,
    checked_vector,
    classify_bedrock_error,
    observe_titan_usage,
)
from horizon_ingestion.observability.genai import embedding_usage
from horizon_ingestion.observability.tracing import Telemetry
from horizon_ingestion.ports.indexing import (
    EmbeddingProtocolError,
    EmbeddingRejectedError,
    EmbeddingUnavailableError,
)

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient


def port_error(error: GenAIError | None) -> Exception:
    """The embedding port's error for a classified provider failure."""
    if isinstance(error, GenAIProviderRejectedError):
        return EmbeddingRejectedError(error.error_code)
    if isinstance(error, GenAIProtocolError):
        return EmbeddingProtocolError(error.error_code)
    if isinstance(error, GenAIProviderUnavailableError):
        return EmbeddingUnavailableError(
            error.error_code, retry_after_seconds=error.retry_after_seconds
        )
    return EmbeddingUnavailableError("provider_unavailable")


@dataclass(frozen=True, slots=True, kw_only=True)
class TitanEmbeddings:
    """The client has SDK retries disabled; this adapter never multiplies attempts."""

    model: Embeddings
    model_id: str
    dimensions: int
    telemetry: Telemetry

    async def embed(self, *, text: str) -> tuple[float, ...]:
        with self.telemetry.work("embedding") as span:
            span.set_attributes(
                {
                    "gen_ai.operation.name": "embeddings",
                    "gen_ai.provider.name": "aws.bedrock",
                    "gen_ai.request.model": self.model_id,
                }
            )
            self.telemetry.measurements.vendor_calls.add(1)
            return await self._attempt(text=text)

    async def _attempt(self, *, text: str) -> tuple[float, ...]:
        try:
            result = await self.model.aembed_query(text)
        except (BotoCoreError, ClientError) as exc:
            raise port_error(classify_bedrock_error(exc)) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            # LangChain parses the 2xx body inside the call, so a body of the wrong shape (a list,
            # a missing key) surfaces here rather than as a provider error.
            raise EmbeddingProtocolError("provider_protocol") from exc
        try:
            return checked_vector(result, dimensions=self.dimensions)
        except GenAIProtocolError as exc:
            raise port_error(exc) from exc


def build_document_embeddings(
    *, client: "BedrockRuntimeClient", model_id: str, dimensions: int, telemetry: Telemetry
) -> TitanEmbeddings:
    """Document embeddings over a borrowed client, built by the policy chat queries use."""
    observe_titan_usage(client=client, on_usage=embedding_usage(telemetry))
    return TitanEmbeddings(
        model=build_bedrock_embeddings(client=client, model_id=model_id, dimensions=dimensions),
        model_id=model_id,
        dimensions=dimensions,
        telemetry=telemetry,
    )
