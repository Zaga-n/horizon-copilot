"""Query embeddings for retrieval, built by the same policy ingestion embeds documents with."""

from typing import TYPE_CHECKING

from langchain_core.embeddings import Embeddings

from horizon_genai import build_bedrock_embeddings

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient


def build_query_embeddings(
    *, client: "BedrockRuntimeClient", model_id: str, dimensions: int
) -> Embeddings:
    """Titan query embeddings over a borrowed client; vectors match the indexed documents."""
    return build_bedrock_embeddings(client=client, model_id=model_id, dimensions=dimensions)
