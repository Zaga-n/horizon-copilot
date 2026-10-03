"""Every agent, provider and database failure ends a turn with a named outcome."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from botocore.exceptions import ClientError
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_chat.api.dependencies import get_runtime
from horizon_chat.application._turn_stream import failure_category
from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import Settings
from horizon_chat.db.readiness import DatabaseReadiness
from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.db.transactions import translate_database_error
from horizon_chat.domain.runs import CheckpointStart, FailureCategory
from horizon_chat.genai.horizon_agent.runner import LangChainHorizonAgent
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG, Scope
from horizon_chat.ports.agent import (
    AgentBudgetError,
    AgentIndexIntegrityError,
    AgentOutputError,
    AgentProtocolError,
    AgentRejectedError,
    AgentUnavailableError,
)
from horizon_chat.ports.checkpoints import CheckpointStoreUnavailableError
from horizon_chat.ports.conversations import (
    ConversationStoreIntegrityError,
    ConversationStoreRejectedError,
    ConversationStoreUnavailableError,
)
from horizon_chat_testing.agent import RecordingIndex, accepted, guardrail, runner
from horizon_chat_testing.models import ScriptedModel
from horizon_chat_testing.telemetry import quiet_telemetry


def bedrock_error(code: str, status: int, *, message: str = "PRIVATE provider text") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {},
                "HostId": "",
                "RequestId": "",
                "RetryAttempts": 0,
            },
        },
        "Converse",
    )


async def drain(agent: LangChainHorizonAgent) -> None:
    admission = accepted()
    start = CheckpointStart(conversation_id=admission.run.conversation_id, checkpoint_id=None)
    async for _ in agent.answer(subject="alice", admission=admission, start=start):
        pass


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        pytest.param(
            bedrock_error("ValidationException", 400), AgentRejectedError, id="context-exceeded"
        ),
        pytest.param(
            bedrock_error("ThrottlingException", 429), AgentUnavailableError, id="throttled"
        ),
        pytest.param(
            bedrock_error("AccessDeniedException", 403), AgentUnavailableError, id="access-denied"
        ),
    ],
)
async def test_provider_rejection_is_distinct_from_unavailability(
    error: ClientError, outcome: type[Exception]
) -> None:
    agent = runner(
        decision=ScriptedModel(script=[error], disable_streaming=True),
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(script=[guardrail(Scope.IN_SCOPE)], disable_streaming=True),
        index=RecordingIndex(hits=()),
    )
    with pytest.raises(outcome):
        await drain(agent)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(AIMessage(content="plain text, no decision"), id="missing-decision"),
        pytest.param(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ScopeDecision",
                        "args": {"scope": "not-a-scope"},
                        "id": "guardrail",
                        "type": "tool_call",
                    }
                ],
            ),
            id="invalid-decision",
        ),
    ],
)
async def test_unparseable_guardrail_output_is_a_named_provider_failure(reply: AIMessage) -> None:
    agent = runner(
        decision=ScriptedModel(script=[], disable_streaming=True),
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(script=[reply], disable_streaming=True),
        index=RecordingIndex(hits=()),
    )
    with pytest.raises(AgentProtocolError, match="guardrail_protocol") as caught:
        await drain(agent)
    assert failure_category(caught.value) == FailureCategory.PROVIDER_PROTOCOL


@dataclass(slots=True)
class FailingEmbeddings(Embeddings):
    """Every query embedding fails with `error`; counts the physical attempts."""

    error: Exception
    calls: int = 0

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        raise self.error

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat never embeds documents")


@pytest.mark.parametrize(
    ("error", "outcome", "category", "attempts"),
    [
        pytest.param(
            bedrock_error("ValidationException", 400),
            AgentRejectedError,
            FailureCategory.PROVIDER_REJECTED,
            1,
            id="embedding-rejected",
        ),
        pytest.param(
            ValueError("Error raised by inference endpoint"),
            AgentProtocolError,
            FailureCategory.PROVIDER_PROTOCOL,
            1,
            id="embedding-protocol",
        ),
        pytest.param(
            AttributeError("'list' object has no attribute 'get'"),
            AgentProtocolError,
            FailureCategory.PROVIDER_PROTOCOL,
            1,
            id="embedding-list-body",
        ),
        pytest.param(
            bedrock_error("ThrottlingException", 429),
            AgentUnavailableError,
            FailureCategory.SERVICE_UNAVAILABLE,
            3,
            id="embedding-throttled",
        ),
    ],
)
async def test_embedding_failures_keep_their_class_and_only_outages_retry(
    error: Exception, outcome: type[Exception], category: FailureCategory, attempts: int
) -> None:
    embeddings = FailingEmbeddings(error=error)
    agent = runner(
        decision=ScriptedModel(script=[AIMessage(content="Draft")], disable_streaming=True),
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(
            script=[guardrail(Scope.IN_SCOPE, requires_search=True)], disable_streaming=True
        ),
        index=RecordingIndex(hits=()),
        retries=3,
        embeddings=embeddings,
    )
    with pytest.raises(outcome) as caught:
        await drain(agent)
    assert failure_category(caught.value) == category
    assert embeddings.calls == attempts


@pytest.mark.parametrize(
    ("error", "outcome", "category"),
    [
        pytest.param(
            bedrock_error(
                "ValidationException", 400, message="The provided model identifier is invalid."
            ),
            AgentUnavailableError,
            FailureCategory.SERVICE_UNAVAILABLE,
            id="unknown-model-id",
        ),
        pytest.param(
            ValueError("Error raised by inference endpoint"),
            AgentProtocolError,
            FailureCategory.PROVIDER_PROTOCOL,
            id="malformed-2xx",
        ),
    ],
)
async def test_model_failures_keep_their_class_and_are_not_retried(
    error: Exception, outcome: type[Exception], category: FailureCategory
) -> None:
    decision = ScriptedModel(script=[error], disable_streaming=True)
    agent = runner(
        decision=decision,
        final=ScriptedModel(script=[], tags=[FINAL_TAG]),
        utility=ScriptedModel(script=[guardrail(Scope.IN_SCOPE)], disable_streaming=True),
        index=RecordingIndex(hits=()),
        retries=3,
    )
    with pytest.raises(outcome) as caught:
        await drain(agent)
    assert failure_category(caught.value) == category
    assert len(decision.seen) == 1


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (AgentBudgetError("agent_budget"), FailureCategory.BUDGET_EXHAUSTED),
        (AgentOutputError("invalid_citations"), FailureCategory.INVALID_CITATIONS),
        (AgentProtocolError("provider_protocol"), FailureCategory.PROVIDER_PROTOCOL),
        (AgentIndexIntegrityError("index_integrity"), FailureCategory.INDEX_INTEGRITY),
        (AgentRejectedError("provider_rejected"), FailureCategory.PROVIDER_REJECTED),
        (AgentUnavailableError("agent_unavailable"), FailureCategory.SERVICE_UNAVAILABLE),
        (
            ConversationStoreUnavailableError("database_unavailable"),
            FailureCategory.SERVICE_UNAVAILABLE,
        ),
        (
            CheckpointStoreUnavailableError("checkpoint_unavailable"),
            FailureCategory.SERVICE_UNAVAILABLE,
        ),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_agent_failures_have_named_categories(error: Exception, category: FailureCategory) -> None:
    assert failure_category(error) == category


def driver(
    error: type[DBAPIError], *, invalidated: bool = False, cause: Exception | None = None
) -> DBAPIError:
    return error("SELECT 1", {}, cause or Exception("driver"), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(driver(IntegrityError), ConversationStoreIntegrityError, id="constraint"),
        pytest.param(driver(DataError), ConversationStoreRejectedError, id="data"),
        pytest.param(driver(OperationalError), ConversationStoreUnavailableError, id="operational"),
        pytest.param(driver(InterfaceError), ConversationStoreUnavailableError, id="interface"),
        pytest.param(PoolTimeoutError(), ConversationStoreUnavailableError, id="pool-timeout"),
        pytest.param(ConnectionResetError(), ConversationStoreUnavailableError, id="os-error"),
        pytest.param(
            driver(DBAPIError, invalidated=True),
            ConversationStoreUnavailableError,
            id="invalidated",
        ),
        pytest.param(
            driver(ProgrammingError, cause=InsufficientPrivilege("permission denied")),
            ConversationStoreUnavailableError,
            id="insufficient-privilege",
        ),
    ],
)
def test_database_failures_are_classified(failure: Exception, expected: type[Exception]) -> None:
    assert type(translate_database_error(failure)) is expected


def test_programming_errors_are_not_relabelled() -> None:
    assert translate_database_error(driver(ProgrammingError)) is None


@asynccontextmanager
async def exhausted_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("postgresql+psycopg://chat@127.0.0.1:1/horizon")

    def no_connection(self: AsyncEngine) -> object:
        raise PoolTimeoutError("QueuePool limit reached")

    monkeypatch.setattr(AsyncEngine, "connect", no_connection)
    try:
        yield engine
    finally:
        monkeypatch.undo()
        await engine.dispose()


async def test_an_exhausted_retrieval_pool_ends_the_turn_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with exhausted_pool(monkeypatch) as engine:
        agent = runner(
            decision=ScriptedModel(script=[AIMessage(content="Draft")], disable_streaming=True),
            final=ScriptedModel(script=[], tags=[FINAL_TAG]),
            utility=ScriptedModel(
                script=[guardrail(Scope.IN_SCOPE, requires_search=True)], disable_streaming=True
            ),
            index=PgvectorEvidenceIndex(
                engine=engine, embedding_model_id="titan", telemetry=quiet_telemetry()
            ),
            embeddings=FullDimensionEmbeddings(),
            dimensions=1024,
        )
        with pytest.raises(AgentUnavailableError) as caught:
            await drain(agent)
    assert failure_category(caught.value) == FailureCategory.SERVICE_UNAVAILABLE


class FullDimensionEmbeddings(Embeddings):
    """A compatible query vector, so the search reaches the exhausted pool."""

    def embed_query(self, text: str) -> list[float]:
        return [1.0, *(0.0 for _ in range(1023))]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("chat never embeds documents")


async def test_ready_reports_unavailable_when_the_pool_is_exhausted(
    monkeypatch: pytest.MonkeyPatch, chat_settings: Settings
) -> None:
    async with exhausted_pool(monkeypatch) as engine:
        readiness = DatabaseReadiness(
            engine=engine,
            # Never opened: the checkpoint pool is consulted only after the app database answers.
            checkpoints=AsyncConnectionPool("postgresql://chat@127.0.0.1:1/horizon", open=False),
            embedding_model_id="titan",
        )
        app = create_app(settings=chat_settings)
        app.dependency_overrides[get_runtime] = lambda: ProbeRuntime(readiness=readiness)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unready"}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeRuntime:
    readiness: DatabaseReadiness
    readiness_timeout_seconds: float = 1.0
