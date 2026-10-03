"""Bounded retrieval inputs and immutable evidence metadata."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from horizon_chat.domain.sources import Source
from horizon_chat.domain.values import StrictModel

MAX_QUERY_CHARS = 2000


class SearchInput(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)]
    k: Annotated[int, Field(ge=1, le=8)] = 5


class SearchHit(StrictModel):
    source: Source
    excerpt: str
    score: Annotated[float, Field(allow_inf_nan=False)]


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalPolicy:
    """Limits supplied by deployment policy, never model-controlled filters."""

    max_chunks: int
    max_excerpt_chars: int
    max_evidence_chars: int


class RetrievalUnavailableError(Exception):
    """A transient embedding, rewrite, or index dependency failure."""


class RetrievalRejectedError(Exception):
    """The embedding provider refused this query itself; resending it fails the same way."""


class RetrievalInvalidOutputError(Exception):
    """A successful embedding response carried no usable vector."""


class RetrievalIntegrityError(Exception):
    """Index or provider output violates the configured embedding/read contract."""
