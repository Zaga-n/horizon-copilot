"""Query rewrite fallback and embedding contract with controlled model responses."""

from dataclasses import dataclass, field

import pytest
from botocore.exceptions import ClientError
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from horizon_chat.domain.retrieval import (
    RetrievalInvalidOutputError,
    RetrievalPolicy,
    RetrievalRejectedError,
    RetrievalUnavailableError,
    SearchHit,
)
from horizon_chat.genai.retrieval.retriever import Retriever
from horizon_chat_testing.telemetry import quiet_telemetry


@dataclass
class RecordingEmbeddings(Embeddings):
    """Records the exact search query while returning a configured embedding."""

    vector: list[float]
    queries: list[str] = field(default_factory=list)

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self.vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat must never embed uploaded documents")


class NoIndex:
    async def search(
        self, *, subject: str, vector: tuple[float, ...], k: int, policy: RetrievalPolicy
    ) -> tuple[SearchHit, ...]:
        return ()


def retriever(*, rewrite: str, embeddings: Embeddings, dimensions: int) -> Retriever:
    return Retriever(
        rewrite_model=GenericFakeChatModel(messages=iter([rewrite])),
        embeddings=embeddings,
        index=NoIndex(),
        policy=RetrievalPolicy(max_chunks=5, max_excerpt_chars=100, max_evidence_chars=500),
        dimensions=dimensions,
        embedding_model_id="test-embeddings",
        telemetry=quiet_telemetry(),
    )


async def test_rewrite_is_bounded_before_embedding() -> None:
    embeddings = RecordingEmbeddings(vector=[1.0, 0.0])
    encoder = retriever(rewrite="q" * 3000, embeddings=embeddings, dimensions=2)
    vector = await encoder.encode(query="Horizon", context="Earlier project", rewrite=True)
    assert vector == (1.0, 0.0)
    assert embeddings.queries == ["q" * 2000]


async def test_blank_rewrite_falls_back_to_original() -> None:
    embeddings = RecordingEmbeddings(vector=[1.0])
    encoder = retriever(rewrite="   ", embeddings=embeddings, dimensions=1)
    await encoder.encode(query="Original question", context="", rewrite=True)
    assert embeddings.queries == ["Original question"]


async def test_provider_vector_dimensions_are_validated() -> None:
    encoder = retriever(
        rewrite="Horizon", embeddings=RecordingEmbeddings(vector=[1.0]), dimensions=1024
    )
    with pytest.raises(RetrievalInvalidOutputError, match="embedding_protocol"):
        await encoder.encode(query="Horizon", context="", rewrite=True)


async def test_exhausted_rewrite_budget_embeds_the_bounded_original() -> None:
    embeddings = RecordingEmbeddings(vector=[1.0])
    encoder = retriever(rewrite="unused", embeddings=embeddings, dimensions=1)
    await encoder.encode(query="x" * 3000, context="", rewrite=False)
    assert embeddings.queries == ["x" * 2000]


@dataclass
class FailingEmbeddings(Embeddings):
    """The provider call fails with `error`, as LangChain surfaces it."""

    error: Exception

    def embed_query(self, text: str) -> list[float]:
        raise self.error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat must never embed uploaded documents")


def bedrock_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ValueError("Error raised by inference endpoint"),
            RetrievalInvalidOutputError,
            id="unparseable-body",
        ),
        pytest.param(
            AttributeError("'list' object has no attribute 'get'"),
            RetrievalInvalidOutputError,
            id="list-body",
        ),
        pytest.param(
            bedrock_error("ValidationException", "Input is too long for requested model."),
            RetrievalRejectedError,
            id="rejected",
        ),
        pytest.param(
            bedrock_error("ValidationException", "The provided model identifier is invalid."),
            RetrievalUnavailableError,
            id="unknown-model-id",
        ),
        pytest.param(
            bedrock_error("ThrottlingException", "Rate exceeded"),
            RetrievalUnavailableError,
            id="throttled",
        ),
    ],
)
async def test_embedding_failures_keep_their_class(
    error: Exception, expected: type[Exception]
) -> None:
    encoder = retriever(rewrite="unused", embeddings=FailingEmbeddings(error=error), dimensions=1)
    with pytest.raises(expected):
        await encoder.encode(query="Horizon", context="", rewrite=False)


class StringVectorEmbeddings(Embeddings):
    """A 2xx body whose vector elements are strings, which the SDK passes through unchecked."""

    def embed_query(self, text: str) -> list[float]:
        return ["0.6", "0.8"]  # type: ignore[list-item]  # the untrusted shape under test

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat must never embed uploaded documents")


async def test_non_numeric_vector_elements_are_invalid_output() -> None:
    encoder = retriever(rewrite="unused", embeddings=StringVectorEmbeddings(), dimensions=2)
    with pytest.raises(RetrievalInvalidOutputError, match="embedding_protocol"):
        await encoder.encode(query="Horizon", context="", rewrite=False)
