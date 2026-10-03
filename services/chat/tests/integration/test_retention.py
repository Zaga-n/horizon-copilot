"""Replica exclusion, split-store crash recovery, and retention preservation on PostgreSQL."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_chat.bootstrap.supervisor import LoopPolicy, ProcessHealth, run_supervised
from horizon_chat.db.checkpoints import PostgresCheckpointStore
from horizon_chat.db.retention import JOB_LOCK, ConversationRetention, RetentionPass
from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.feedback import FeedbackInput, Rating
from horizon_chat.domain.runs import AttemptStatus, FailureCategory, RunIdentity
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.checkpoints import (
    CheckpointStore,
    CheckpointStoreIntegrityError,
    CheckpointStoreUnavailableError,
)
from horizon_chat.ports.errors import DependencyUnavailableError
from horizon_chat.workers.retention import retention_iteration
from horizon_chat_testing.ledger import Ledger, ledger
from horizon_migrations.db.checkpoints import setup_checkpoints

pytestmark = pytest.mark.integration
GRANTS_SQL = (
    Path(__file__).resolve().parents[4] / "services/migrations/sql/runtime_grants.sql"
).read_text()


@dataclass(frozen=True, slots=True, kw_only=True)
class Harness:
    """Real stores and a privileged seed connection in one disposable database."""

    chat: Ledger
    retention: ConversationRetention
    checkpoints: PostgresCheckpointStore
    admin: psycopg.AsyncConnection[tuple[object, ...]]
    telemetry: Telemetry
    exporter: InMemorySpanExporter


@pytest.fixture
async def harness(migrated_database: str) -> AsyncIterator[Harness]:
    exporter = InMemorySpanExporter()
    provider, meters = TracerProvider(), MeterProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry(
        tracer=provider.get_tracer("test"),
        measurements=Measurements(meter=meters.get_meter("test")),
    )
    await setup_checkpoints(
        dsn=SecretStr(make_conninfo(migrated_database, options="-crole=checkpoint_migrator")),
        expected_role="checkpoint_migrator",
    )
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as admin:
        await admin.execute(GRANTS_SQL)
        engine = create_async_engine(
            make_url(migrated_database).set(drivername="postgresql+psycopg"),
            connect_args={"options": "-crole=chat_runtime -ctimezone=UTC"},
        )
        try:
            async with await psycopg.AsyncConnection.connect(
                migrated_database,
                autocommit=True,
                row_factory=dict_row,
                options="-crole=chat_runtime -csearch_path=langgraph",
            ) as conn:
                checkpoints = PostgresCheckpointStore(saver=AsyncPostgresSaver(conn))
                yield Harness(
                    chat=ledger(engine),
                    retention=job(engine=engine, checkpoints=checkpoints, telemetry=telemetry),
                    checkpoints=checkpoints,
                    admin=admin,
                    telemetry=telemetry,
                    exporter=exporter,
                )
        finally:
            await engine.dispose()
            provider.shutdown()
            meters.shutdown()


def job(
    *,
    engine: AsyncEngine,
    checkpoints: CheckpointStore,
    telemetry: Telemetry,
    batch_size: int = 1,
) -> ConversationRetention:
    return ConversationRetention(
        engine=engine,
        checkpoints=checkpoints,
        retention_days=30,
        batch_size=batch_size,
        checkpoint_timeout_seconds=5,
        telemetry=telemetry,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DiesAfterCheckpointDelete:
    """The process dies after removing the thread, before deleting the ledger rows."""

    store: PostgresCheckpointStore

    async def current_id(self, *, conversation_id: UUID) -> str | None:
        return await self.store.current_id(conversation_id=conversation_id)

    async def require_checkpoint(self, *, conversation_id: UUID, checkpoint_id: str) -> None:
        await self.store.require_checkpoint(
            conversation_id=conversation_id, checkpoint_id=checkpoint_id
        )

    async def delete_thread(self, *, conversation_id: UUID) -> None:
        await self.store.delete_thread(conversation_id=conversation_id)
        raise ProcessDiedError


class ProcessDiedError(Exception):
    pass


def run_identity() -> RunIdentity:
    return RunIdentity(
        id=uuid4(),
        assistant_message_id=uuid4(),
        trace_id=uuid4().hex,
        root_span_id=uuid4().hex[:16],
        agent_version="test",
        prompt_version="test",
        retrieval_version="test",
    )


async def expire(harness: Harness, conversation_id: UUID) -> None:
    await harness.admin.execute(
        "UPDATE app.conversations SET last_activity_at = now() - interval '31 days' WHERE id = %s",
        (conversation_id,),
    )


async def test_split_store_crash_resumes_with_purge_fence(harness: Harness) -> None:
    conversation = await harness.chat.conversations.create(subject="alice")

    async def answer(state: MessagesState) -> MessagesState:
        return {"messages": [AIMessage(content="Checkpoint answer")]}

    builder = StateGraph(MessagesState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile(checkpointer=harness.checkpoints.saver)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="Horizon")]},
        {"configurable": {"thread_id": str(conversation.id)}},
    )
    assert await harness.checkpoints.current_id(conversation_id=conversation.id) is not None
    accepted = await harness.chat.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="first",
        content="Horizon",
        identity=run_identity(),
    )
    await harness.chat.runs.finish(
        subject="alice",
        run_id=accepted.run.id,
        status=AttemptStatus.FAILED,
        content="Answer",
        sources=(),
        failure_category=FailureCategory.SERVICE_UNAVAILABLE,
    )
    retry = await harness.chat.runs.retry(
        subject="alice",
        conversation_id=conversation.id,
        turn_id=accepted.run.turn_id,
        expected_run_id=accepted.run.id,
        request_key="retry",
        identity=run_identity(),
    )
    await harness.chat.runs.finish(
        subject="alice",
        run_id=retry.run.id,
        status=AttemptStatus.COMPLETED,
        content="Answer",
        sources=(),
        failure_category=None,
    )
    await harness.chat.feedback.put_answer_feedback(
        subject="alice",
        message_id=retry.run.assistant_message_id,
        request=FeedbackInput(rating=Rating.LIKE),
    )
    await harness.chat.feedback.put_thread_feedback(
        subject="alice",
        conversation_id=conversation.id,
        request=FeedbackInput(comment="Feedback"),
    )
    await expire(harness, conversation.id)
    crashing = job(
        engine=harness.chat.engine,
        checkpoints=DiesAfterCheckpointDelete(store=harness.checkpoints),
        telemetry=harness.telemetry,
    )
    with pytest.raises(ProcessDiedError):
        await crashing.run_once()
    assert await harness.checkpoints.current_id(conversation_id=conversation.id) is None
    with pytest.raises(ConversationNotFoundError):
        await harness.chat.conversations.detail(subject="alice", conversation_id=conversation.id)
    with pytest.raises(ConversationNotFoundError):
        await harness.chat.runs.admit(
            subject="alice",
            conversation_id=conversation.id,
            request_key="racing",
            content="Follow-up",
            identity=run_identity(),
        )
    # The crashed pass released its lock; the next pass finishes the PURGING row.
    assert (await harness.retention.run_once()).purged == 1
    async with harness.chat.engine.connect() as conn:
        for table in (
            "conversations",
            "messages",
            "turns",
            "agent_runs",
            "retry_requests",
            "thread_feedback",
            "message_feedback",
        ):
            assert await conn.scalar(text(f"SELECT count(*) FROM app.{table}")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM app.users")) == 1
    assert await harness.checkpoints.current_id(conversation_id=conversation.id) is None
    assert (await harness.retention.run_once()).purged == 0


async def test_only_one_replica_purges_while_the_lock_is_held(harness: Harness) -> None:
    conversations = [await harness.chat.conversations.create(subject="alice") for _ in range(3)]
    for conversation in conversations:
        await expire(harness, conversation.id)
    lock = "SELECT {}(hashtext(%s))"
    await harness.admin.execute(lock.format("pg_advisory_lock"), (JOB_LOCK,))
    assert (await harness.retention.run_once()).purged == 0
    for conversation in conversations:
        assert (
            await harness.chat.conversations.detail(
                subject="alice", conversation_id=conversation.id
            )
        ).id
    await harness.admin.execute(lock.format("pg_advisory_unlock"), (JOB_LOCK,))
    retention = job(
        engine=harness.chat.engine,
        checkpoints=harness.checkpoints,
        telemetry=harness.telemetry,
        batch_size=3,
    )
    async with asyncio.timeout(10):
        passes = await asyncio.gather(*(retention.run_once() for _ in range(4)))
    assert sorted(result.purged for result in passes) == [0, 0, 0, 3]
    held = await harness.admin.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
    )
    assert await held.fetchone() == (0,)


async def test_active_turn_and_recent_activity_protect_conversations(harness: Harness) -> None:
    active = await harness.chat.conversations.create(subject="alice")
    await harness.chat.runs.admit(
        subject="alice",
        conversation_id=active.id,
        request_key="active",
        content="Horizon",
        identity=run_identity(),
    )
    await expire(harness, active.id)
    recent = await harness.chat.conversations.create(subject="alice")
    await harness.admin.execute(
        "UPDATE app.conversations SET created_at = now() - interval '90 days' WHERE id = %s",
        (recent.id,),
    )
    assert (await harness.retention.run_once()).purged == 0
    assert (
        await harness.chat.conversations.detail(subject="alice", conversation_id=active.id)
    ).id == active.id
    assert (
        await harness.chat.conversations.detail(subject="alice", conversation_id=recent.id)
    ).id == recent.id


async def test_checkpoint_failure_keeps_purge_marker_for_retry(harness: Harness) -> None:
    conversation = await harness.chat.conversations.create(subject="alice")
    await expire(harness, conversation.id)
    # Real permission failure proves the app does not cascade after unsuccessful checkpoint deletion.
    await harness.admin.execute("REVOKE DELETE ON ALL TABLES IN SCHEMA langgraph FROM chat_runtime")
    with pytest.raises(CheckpointStoreUnavailableError, match="checkpoint_unavailable"):
        await harness.retention.run_once()
    failed_span = harness.exporter.get_finished_spans()[-1]
    assert failed_span.name == "app.maintenance" and failed_span.status.status_code.name == "ERROR"
    assert (
        failed_span.attributes
        and failed_span.attributes["error.type"] == "CheckpointStoreUnavailableError"
    )
    with pytest.raises(ConversationNotFoundError):
        await harness.chat.conversations.detail(subject="alice", conversation_id=conversation.id)
    await harness.admin.execute(GRANTS_SQL)
    assert (await harness.retention.run_once()).purged == 1

    assert harness.exporter.get_finished_spans()[-1].status.status_code.name == "UNSET"


async def test_retention_preserves_published_document_and_chunk(harness: Harness) -> None:
    conversation = await harness.chat.conversations.create(subject="alice")
    document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4()
    async with harness.admin.transaction():
        await harness.admin.execute(
            """INSERT INTO app.documents (id, user_id, filename, file_type)
            SELECT %s, id, 'project.pdf', 'pdf' FROM app.users WHERE subject = 'alice'""",
            (document_id,),
        )
        await harness.admin.execute(
            """INSERT INTO app.document_versions
            (id, document_id, status, sha256, pipeline_fingerprint, object_key,
            object_version_id, embedding_model_id, embedding_dimensions)
            VALUES (%s, %s, 'published', %s, %s, 'immutable.pdf', 'v1', 'titan', 1024)""",
            (version_id, document_id, "a" * 64, "b" * 64),
        )
        await harness.admin.execute(
            """INSERT INTO app.document_chunks
            (id, version_id, ordinal, text, embedding, filename, file_type, status, embedding_model_id)
            VALUES (%s, %s, 0, 'Evidence', %s::vector, 'project.pdf', 'pdf', 'completed', 'titan')""",
            (chunk_id, version_id, "[" + ",".join(["1"] + ["0"] * 1023) + "]"),
        )
        await harness.admin.execute(
            "UPDATE app.documents SET published_version_id = %s WHERE id = %s",
            (version_id, document_id),
        )
    await expire(harness, conversation.id)
    assert (await harness.retention.run_once()).purged == 1
    cursor = await harness.admin.execute(
        """SELECT d.id, v.id, c.id, c.text, v.object_key, v.object_version_id
        FROM app.documents d JOIN app.document_versions v ON v.id = d.published_version_id
        JOIN app.document_chunks c ON c.version_id = v.id""",
    )
    assert await cursor.fetchall() == [
        (document_id, version_id, chunk_id, "Evidence", "immutable.pdf", "v1")
    ]


@dataclass(slots=True, kw_only=True)
class CorruptThread:
    """One conversation's checkpoint data is inconsistent; records the purge order."""

    store: PostgresCheckpointStore
    corrupt: UUID
    attempted: list[UUID]

    async def current_id(self, *, conversation_id: UUID) -> str | None:
        return await self.store.current_id(conversation_id=conversation_id)

    async def require_checkpoint(self, *, conversation_id: UUID, checkpoint_id: str) -> None:
        await self.store.require_checkpoint(
            conversation_id=conversation_id, checkpoint_id=checkpoint_id
        )

    async def delete_thread(self, *, conversation_id: UUID) -> None:
        self.attempted.append(conversation_id)
        if conversation_id == self.corrupt:
            raise CheckpointStoreIntegrityError("checkpoint_database_error")
        await self.store.delete_thread(conversation_id=conversation_id)


async def test_one_corrupt_checkpoint_does_not_block_other_purges(harness: Harness) -> None:
    corrupt = await harness.chat.conversations.create(subject="alice")
    healthy = await harness.chat.conversations.create(subject="alice")
    later = await harness.chat.conversations.create(subject="alice")
    await expire(harness, corrupt.id)
    await expire(harness, healthy.id)
    store = CorruptThread(store=harness.checkpoints, corrupt=corrupt.id, attempted=[])
    retention = job(engine=harness.chat.engine, checkpoints=store, telemetry=harness.telemetry)
    # A batch of failures only is not retried back to back; the failure is deferred.
    assert await retention.run_once() == RetentionPass(purged=0, failed=1, more_due=False)
    assert await retention.run_once() == RetentionPass(purged=1, failed=0, more_due=True)
    assert store.attempted == [corrupt.id, healthy.id]
    with pytest.raises(ConversationNotFoundError):
        await harness.chat.conversations.detail(subject="alice", conversation_id=healthy.id)
    await expire(harness, later.id)
    store.attempted.clear()
    assert (await retention.run_once()).purged == 1
    assert (await retention.run_once()).purged == 0
    assert store.attempted == [later.id, corrupt.id]


@dataclass(slots=True, kw_only=True)
class CountedIteration:
    """The production retention iteration, counting passes and the outages they raised."""

    retention: ConversationRetention
    expected_passes: int = 0
    passes: int = 0
    outages: int = 0
    succeeded: asyncio.Event = field(default_factory=asyncio.Event)
    backed_off: asyncio.Event = field(default_factory=asyncio.Event)
    expected_passes_ran: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self) -> bool:
        self.passes += 1
        if self.passes == self.expected_passes:
            self.expected_passes_ran.set()
        try:
            more_due = await retention_iteration(job=self.retention, readiness=MigratedDatabase())
        except DependencyUnavailableError:
            self.outages += 1
            if self.outages >= 2:
                self.backed_off.set()
            raise
        self.succeeded.set()
        return more_due


class MigratedDatabase:
    async def check(self) -> bool:
        return True


async def test_a_missing_grant_backs_off_and_recovers_without_a_restart(
    harness: Harness,
) -> None:
    conversation = await harness.chat.conversations.create(subject="alice")
    await expire(harness, conversation.id)
    await harness.admin.execute("REVOKE SELECT ON app.conversations FROM chat_runtime")
    health, stop = ProcessHealth(), asyncio.Event()
    iteration = CountedIteration(retention=harness.retention)
    loop = asyncio.create_task(
        run_supervised(
            name="conversation-retention",
            iteration=iteration,
            policy=LoopPolicy(interval_seconds=0.01, max_backoff_seconds=0.05),
            stop=stop,
            health=health,
        )
    )
    try:
        await asyncio.wait_for(iteration.backed_off.wait(), timeout=10)
        assert health.loops_alive() and not loop.done()
        await harness.admin.execute(GRANTS_SQL)
        await asyncio.wait_for(iteration.succeeded.wait(), timeout=10)
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=10)
    assert health.loops_alive()
    with pytest.raises(ConversationNotFoundError):
        await harness.chat.conversations.detail(subject="alice", conversation_id=conversation.id)


async def test_a_backlog_drains_in_consecutive_passes_without_the_interval(
    harness: Harness,
) -> None:
    for _ in range(2):
        await expire(harness, (await harness.chat.conversations.create(subject="alice")).id)
    health, stop = ProcessHealth(), asyncio.Event()
    # Batch size 1: two full batches, then an empty pass that waits for the interval.
    iteration = CountedIteration(retention=harness.retention, expected_passes=3)
    loop = asyncio.create_task(
        run_supervised(
            name="conversation-retention",
            iteration=iteration,
            policy=LoopPolicy(interval_seconds=60, max_backoff_seconds=60),
            stop=stop,
            health=health,
        )
    )
    try:
        await asyncio.wait_for(iteration.expected_passes_ran.wait(), timeout=10)
        await asyncio.sleep(0.05)  # a fourth pass would have to skip the 60 s interval
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=10)
    assert iteration.passes == 3
    async with harness.chat.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM app.conversations")) == 0
