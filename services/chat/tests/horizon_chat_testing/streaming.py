"""Turn streaming over real application and checkpoint stores with a controlled agent."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue, SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_chat.application.submit_turn import Admitted, submit_turn
from horizon_chat.db.checkpoints import PostgresCheckpointStore
from horizon_chat.db.conversations import SqlConversationStore
from horizon_chat.db.feedback import SqlFeedbackStore
from horizon_chat.db.runs import SqlRunLedger
from horizon_chat.domain.agent import (
    AgentEvent,
    AgentProgress,
    AnswerDelta,
    AnswerSources,
)
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import Admission, CheckpointStart, TurnPolicy
from horizon_chat.domain.streaming import StreamEvent
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.agent import AgentUnavailableError
from horizon_chat.ports.identity import InvalidIdentityError
from horizon_chat_testing.ledger import ledger
from horizon_migrations.db.checkpoints import setup_checkpoints

GRANTS = (
    Path(__file__).resolve().parents[4] / "services/migrations/sql/runtime_grants.sql"
).read_text()


@dataclass(slots=True)
class ControlledAgent:
    calls: int = 0
    fail: bool = False
    gate: asyncio.Event | None = None
    progress: AgentProgress | None = None

    async def answer(
        self, *, subject: str, admission: Admission, start: CheckpointStart
    ) -> AsyncGenerator[AgentEvent]:
        self.calls += 1
        if self.progress is not None:
            yield self.progress
        yield AnswerDelta(text="Visible prefix. ")
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise AgentUnavailableError("PRIVATE provider payload")
        yield AnswerDelta(text="Final answer.")
        yield AnswerSources(sources=())


class Identity:
    async def verify(self, *, token: str | None) -> str:
        if token != "alice":
            raise InvalidIdentityError()
        return token


POLICY = TurnPolicy(
    agent_version="test",
    prompt_version="test",
    retrieval_version="test",
    persistence_attempts=2,
    persistence_backoff_seconds=0.001,
    persistence_max_backoff_seconds=4,
    cleanup_timeout_seconds=2,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamingRuntime:
    """The chat API's runtime view over real stores and a controlled agent."""

    conversations: SqlConversationStore
    runs: SqlRunLedger
    feedback: SqlFeedbackStore
    checkpoints: PostgresCheckpointStore
    agent: ControlledAgent
    telemetry: Telemetry
    recovery: RecoveryBuffer
    turn_policy: TurnPolicy = POLICY
    identity: Identity = field(default_factory=Identity)


async def admit(
    runtime: StreamingRuntime,
    *,
    subject: str,
    conversation_id: UUID,
    request_key: str,
    content: str,
) -> tuple[Admission, AsyncIterator[StreamEvent]]:
    submission = await submit_turn(
        runs=runtime.runs,
        conversations=runtime.conversations,
        checkpoints=runtime.checkpoints,
        agent=runtime.agent,
        recovery=runtime.recovery,
        telemetry=runtime.telemetry,
        policy=runtime.turn_policy,
        subject=subject,
        conversation_id=conversation_id,
        request_key=request_key,
        content=content,
    )
    if not isinstance(submission, Admitted):
        pytest.fail("expected a new attempt, not a replay")
    return submission.admission, submission.events


@asynccontextmanager
async def open_streaming(
    migrated_database: str,
) -> AsyncIterator[tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter]]:
    """Real ledger and checkpoint stores with a controlled agent and in-memory spans."""
    await setup_checkpoints(dsn=SecretStr(migrated_database), expected_role="postgres")
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute(GRANTS)
    engine = create_async_engine(make_url(migrated_database).set(drivername="postgresql+psycopg"))
    pool = AsyncConnectionPool[psycopg.AsyncConnection[DictRow]](
        migrated_database,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "options": "-csearch_path=langgraph"},
    )
    await pool.open(wait=True)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    metrics = MeterProvider()
    agent = ControlledAgent()
    stores = ledger(engine)
    try:
        yield (
            StreamingRuntime(
                conversations=stores.conversations,
                runs=stores.runs,
                feedback=stores.feedback,
                checkpoints=PostgresCheckpointStore(saver=AsyncPostgresSaver(pool)),
                agent=agent,
                telemetry=Telemetry(
                    tracer=provider.get_tracer("test"),
                    measurements=Measurements(meter=metrics.get_meter("test")),
                ),
                recovery=RecoveryBuffer(),
            ),
            agent,
            exporter,
        )
    finally:
        await pool.close()
        await engine.dispose()
        provider.shutdown()
        metrics.shutdown()


def events(body: str) -> list[dict[str, JsonValue]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
