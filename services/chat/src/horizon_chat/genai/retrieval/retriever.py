"""Query rewrite, embedding, and authorized evidence search for any agent that needs it."""

from dataclasses import dataclass
from typing import Protocol

from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from horizon_chat.domain.retrieval import (
    MAX_QUERY_CHARS,
    RetrievalInvalidOutputError,
    RetrievalPolicy,
    RetrievalRejectedError,
    RetrievalUnavailableError,
    SearchHit,
    SearchInput,
)
from horizon_chat.genai.retrieval.prompts import PROMPT_VERSION, REWRITE_PROMPT
from horizon_chat.observability.tracing import Telemetry
from horizon_genai import (
    GenAIProtocolError,
    GenAIProviderRejectedError,
    checked_vector,
    classify_bedrock_error,
)

RETRIEVAL_VERSION = "pgvector-v1"  # bump when rewrite, embedding or ranking behavior changes


def embedding_error(exc: Exception) -> Exception:
    """The retrieval error for a query-embedding provider failure, keeping its class.

    Every cause stops this search; a malformed or refused response is not index damage, and
    only an unavailable provider is worth retrying.
    """
    error = classify_bedrock_error(exc)
    if isinstance(error, GenAIProtocolError):
        return RetrievalInvalidOutputError("embedding_protocol")
    if isinstance(error, GenAIProviderRejectedError):
        return RetrievalRejectedError("embedding_rejected")
    return RetrievalUnavailableError("embedding_unavailable")


class EvidenceIndex(Protocol):
    """Implemented by the pgvector index in db/, which genai/ may not import."""

    async def search(
        self,
        *,
        subject: str,
        vector: tuple[float, ...],
        k: int,
        policy: RetrievalPolicy,
    ) -> tuple[SearchHit, ...]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Retriever:
    """Rewrite failure falls back to the bounded original; embedding failure aborts search."""

    rewrite_model: BaseChatModel
    embeddings: Embeddings
    index: EvidenceIndex
    policy: RetrievalPolicy
    dimensions: int
    embedding_model_id: str
    telemetry: Telemetry

    async def search(
        self, *, subject: str, request: SearchInput, context: str, rewrite: bool
    ) -> tuple[SearchHit, ...]:
        vector = await self.encode(query=request.query, context=context, rewrite=rewrite)
        return await self.index.search(
            subject=subject,
            vector=vector,
            k=min(request.k, self.policy.max_chunks),
            policy=self.policy,
        )

    async def encode(self, *, query: str, context: str, rewrite: bool) -> tuple[float, ...]:
        with self.telemetry.work("embedding") as span:
            span.set_attributes(
                {
                    "gen_ai.operation.name": "embeddings",
                    "gen_ai.provider.name": "aws.bedrock",
                    "gen_ai.request.model": self.embedding_model_id,
                    "gen_ai.embeddings.dimension.count": self.dimensions,
                    "app.prompt.version": PROMPT_VERSION,
                    "app.usage.available": False,
                    "app.cost.available": False,
                }
            )
            bounded = query[:MAX_QUERY_CHARS]
            text = await self._rewrite(query=bounded, context=context) if rewrite else bounded
            try:
                vector = await self.embeddings.aembed_query(text)
            except TimeoutError as exc:
                raise RetrievalUnavailableError("embedding_unavailable") from exc
            except (BotoCoreError, ClientError) as exc:
                raise embedding_error(exc) from exc
            except (AttributeError, TypeError, ValueError) as exc:
                # LangChain parses the 2xx body inside the call, so a body of the wrong shape
                # (a list, a missing key, non-numeric values) surfaces here, not as a ClientError.
                raise RetrievalInvalidOutputError("embedding_protocol") from exc
            try:
                return checked_vector(vector, dimensions=self.dimensions)
            except GenAIProtocolError as exc:
                raise RetrievalInvalidOutputError("embedding_protocol") from exc

    async def _rewrite(self, *, query: str, context: str) -> str:
        try:
            response = await self.rewrite_model.ainvoke(
                [
                    SystemMessage(content=REWRITE_PROMPT),
                    HumanMessage(
                        content=f"Context: {context[:MAX_QUERY_CHARS]}\nQuestion: {query}"
                    ),
                ]
            )
        except (BotoCoreError, ClientError, TimeoutError):
            return query
        return response.text.strip()[:MAX_QUERY_CHARS] or query
