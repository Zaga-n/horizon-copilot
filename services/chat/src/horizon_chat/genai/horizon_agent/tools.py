"""The sole read-only RAG tool; trusted identity comes from the agent context, not the model."""

from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import Field

from horizon_chat.domain.retrieval import (
    MAX_QUERY_CHARS,
    RetrievalIntegrityError,
    RetrievalInvalidOutputError,
    RetrievalRejectedError,
    RetrievalUnavailableError,
    SearchHit,
    SearchInput,
)
from horizon_chat.genai.horizon_agent.schemas import AgentContext, AttemptContext, attempt_context
from horizon_chat.genai.retrieval.retriever import Retriever

RAG_TOOL_NAME = "rag_search"


class SearchUnavailableError(Exception):
    """Private abort recognized by tool retry and translated by the runner."""


class SearchRejectedError(Exception):
    """Private abort when the provider refuses the query itself; never retried."""


class SearchInvalidOutputError(Exception):
    """Private abort when a successful provider response is unusable; never retried."""


class SearchIntegrityError(Exception):
    """Private abort when evidence violates the index contract."""


def build_rag_tool(*, retriever: Retriever) -> BaseTool:
    # Arguments come from the signature (SearchInput forbids the injected runtime).
    @tool(RAG_TOOL_NAME)
    async def rag_search(
        query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)],
        runtime: ToolRuntime[AgentContext],
        k: Annotated[int, Field(ge=1, le=8)] = 5,
    ) -> str:
        """Search accessible published Horizon document evidence; adjust query/k for a second search."""
        context = attempt_context.get()
        if context.searches >= context.search_limit:
            return '{"untrusted_evidence":[],"limit_reached":true}'
        rewrite = context.rewrites < context.rewrite_limit
        if rewrite:
            context.rewrites += 1
        # The embedding call is a physical model attempt too.
        context.debit_model(final=False)
        try:
            hits = await retriever.search(
                subject=runtime.context.subject,
                request=SearchInput(query=query, k=k),
                context=context.conversation_context,
                rewrite=rewrite,
            )
        except RetrievalUnavailableError as exc:
            raise SearchUnavailableError("search_unavailable") from exc
        except RetrievalRejectedError as exc:
            raise SearchRejectedError("search_rejected") from exc
        except RetrievalInvalidOutputError as exc:
            raise SearchInvalidOutputError("search_protocol") from exc
        except RetrievalIntegrityError as exc:
            raise SearchIntegrityError("search_integrity") from exc
        context.searches += 1
        return evidence_payload(
            context=context, hits=hits, max_chars=retriever.policy.max_evidence_chars
        )

    return rag_search


def evidence_payload(
    *, context: AttemptContext, hits: tuple[SearchHit, ...], max_chars: int
) -> str:
    """Assign stable [S<n>] markers once per chunk and bound total evidence per attempt."""
    remaining = max_chars - sum(len(hit.excerpt) for hit in context.hits.values())
    returned = []
    for hit in hits:
        key = str(hit.source.chunk_id)
        if key not in context.hits and remaining > 0:
            source = hit.source.model_copy(update={"marker": f"S{len(context.hits) + 1}"})
            excerpt = hit.excerpt[:remaining]
            context.hits[key] = hit.model_copy(update={"source": source, "excerpt": excerpt})
            remaining -= len(excerpt)
        if key in context.hits:
            returned.append(context.hits[key].model_dump_json())
    return '{"untrusted_evidence":[' + ",".join(returned) + "]}"
