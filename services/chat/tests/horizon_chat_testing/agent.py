"""Real compiled agent graph over scripted models and a controlled evidence index."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import repeat
from uuid import uuid4

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from horizon_chat.domain.retrieval import RetrievalPolicy, SearchHit
from horizon_chat.domain.runs import Admission, AttemptStatus, Run
from horizon_chat.domain.sources import Source
from horizon_chat.genai.horizon_agent.agent import AgentPolicy, build_agent
from horizon_chat.genai.horizon_agent.llms import AgentModels
from horizon_chat.genai.horizon_agent.runner import LangChainHorizonAgent
from horizon_chat.genai.horizon_agent.schemas import Scope
from horizon_chat.genai.retrieval.retriever import EvidenceIndex, Retriever
from horizon_chat_testing.models import ScriptedModel
from horizon_chat_testing.telemetry import quiet_telemetry


class OneDimensionEmbeddings(Embeddings):
    def embed_query(self, text: str) -> list[float]:
        return [1.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat never embeds documents")


@dataclass
class RecordingIndex:
    """Returns controlled untrusted evidence and records the server-derived identity."""

    hits: tuple[SearchHit, ...]
    subjects: list[str] = field(default_factory=list)

    async def search(
        self,
        *,
        subject: str,
        vector: tuple[float, ...],
        k: int,
        policy: RetrievalPolicy,
    ) -> tuple[SearchHit, ...]:
        self.subjects.append(subject)
        return self.hits


def accepted() -> Admission:
    return Admission(
        input="What is project Horizon-X's work package?",
        replayed=False,
        run=Run(
            id=uuid4(),
            conversation_id=uuid4(),
            turn_id=uuid4(),
            user_message_id=uuid4(),
            assistant_message_id=uuid4(),
            attempt_number=1,
            trace_id=uuid4().hex,
            root_span_id=uuid4().hex[:16],
            status=AttemptStatus.PENDING,
            failure_category=None,
            retry_of_run_id=None,
            started_at=datetime.now(UTC),
            ended_at=None,
            agent_version="test",
            prompt_version="test",
            retrieval_version="test",
        ),
    )


def guardrail(scope: Scope, *, requires_search: bool = False) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ScopeDecision",
                "args": {"scope": scope.value, "requires_search": requires_search},
                "id": "guardrail",
                "type": "tool_call",
            }
        ],
    )


def evidence(*, text: str = "WP1 is coordination.") -> SearchHit:
    return SearchHit(
        excerpt=text,
        score=1,
        source=Source(
            marker="raw",
            chunk_id=uuid4(),
            document_id=uuid4(),
            document_version_id=uuid4(),
            title="Project",
            filename="project.pdf",
            file_type="pdf",
            page=12,
        ),
    )


def runner(
    *,
    decision: ScriptedModel,
    final: ScriptedModel,
    utility: ScriptedModel,
    index: EvidenceIndex,
    retries: int = 1,
    summary_tokens: int = 12000,
    keep_messages: int = 8,
    embeddings: Embeddings | None = None,
    dimensions: int = 1,
) -> LangChainHorizonAgent:
    policy = RetrievalPolicy(max_chunks=8, max_excerpt_chars=3000, max_evidence_chars=24000)
    telemetry = quiet_telemetry()
    graph = build_agent(
        models=AgentModels(decision=decision, final=final, utility=utility),
        retriever=Retriever(
            rewrite_model=GenericFakeChatModel(messages=repeat(AIMessage(content="query"))),
            embeddings=embeddings or OneDimensionEmbeddings(),
            index=index,
            policy=policy,
            dimensions=dimensions,
            embedding_model_id="test-embeddings",
            telemetry=telemetry,
        ),
        checkpointer=InMemorySaver(),
        telemetry=telemetry,
        policy=AgentPolicy(
            max_model_calls=5,
            max_tool_calls=2,
            retry_attempts=retries,
            initial_backoff_seconds=0.001,
            max_backoff_seconds=0.001,
            summary_trigger_tokens=summary_tokens,
            summary_keep_messages=keep_messages,
        ),
    )
    return LangChainHorizonAgent(
        telemetry=telemetry,
        graph=graph,
        physical_limit=24,
        rewrite_limit=2,
        search_limit=2,
        deadline_seconds=0.5,
    )
