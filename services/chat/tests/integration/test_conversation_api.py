"""HTTP ownership and feedback serialization over actual PostgreSQL stores."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_chat.api.dependencies import get_chat_runtime
from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import Settings
from horizon_chat.db.conversations import SqlConversationStore
from horizon_chat.db.feedback import SqlFeedbackStore
from horizon_chat.db.runs import SqlRunLedger
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import AttemptStatus, RunIdentity
from horizon_chat.ports.identity import InvalidIdentityError

pytestmark = pytest.mark.integration
GRANTS_SQL = (
    Path(__file__).resolve().parents[4] / "services/migrations/sql/runtime_grants.sql"
).read_text()


class TestIdentity:
    """Opaque test bearers only; actual Google signature validation is a separate test."""

    async def verify(self, *, token: str | None) -> str:
        if token not in ("alice", "bob"):
            raise InvalidIdentityError()
        return token


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiTestRuntime:
    conversations: SqlConversationStore
    runs: SqlRunLedger
    feedback: SqlFeedbackStore
    identity: TestIdentity
    recovery: RecoveryBuffer = field(default_factory=RecoveryBuffer)


@pytest.fixture
async def runtime(migrated_database: str) -> AsyncIterator[ApiTestRuntime]:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as conn:
        await conn.execute(GRANTS_SQL)
    engine = create_async_engine(
        make_url(migrated_database).set(drivername="postgresql+psycopg"),
        connect_args={"options": "-crole=chat_runtime -ctimezone=UTC"},
    )
    try:
        yield ApiTestRuntime(
            conversations=SqlConversationStore(engine=engine),
            runs=SqlRunLedger(engine=engine, turn_lease_seconds=150),
            feedback=SqlFeedbackStore(engine=engine),
            identity=TestIdentity(),
        )
    finally:
        await engine.dispose()


async def test_owned_history_detail_and_feedback_http_flow(
    runtime: ApiTestRuntime, chat_settings: Settings
) -> None:
    app = create_app(settings=chat_settings)
    app.dependency_overrides[get_chat_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/conversations")).status_code == 401
        client.headers["Authorization"] = "Bearer alice"
        created = await client.post("/v1/conversations")
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        path = f"/v1/conversations/{conversation_id}"
        assert (await client.get("/v1/conversations")).json()["conversations"][0][
            "id"
        ] == conversation_id
        assert (await client.get(path + "/messages")).json()["messages"] == []
        assert (
            await client.put(path + "/feedback", json={"comment": "Thread review"})
        ).status_code == 204
        assert (await client.get(path)).json()["feedback"]["comment"] == "Thread review"
        client.headers["Authorization"] = "Bearer bob"
        assert (await client.get(path)).status_code == 404
        assert (await client.get(path + "/messages")).status_code == 404
        assert (await client.put(path + "/feedback", json={"rating": "dislike"})).status_code == 404
        assert (await client.get("/v1/conversations")).json()["conversations"] == []
        client.headers["Authorization"] = "Bearer alice"
        assert (await client.delete(path + "/feedback")).status_code == 204
        assert (await client.get(path)).json()["feedback"] is None


async def test_feedback_cannot_override_execution_joins(
    runtime: ApiTestRuntime, chat_settings: Settings
) -> None:
    app = create_app(settings=chat_settings)
    app.dependency_overrides[get_chat_runtime] = lambda: runtime
    conversation = await runtime.conversations.create(subject="alice")
    identity = RunIdentity(
        id=uuid4(),
        assistant_message_id=uuid4(),
        trace_id=uuid4().hex,
        root_span_id=uuid4().hex[:16],
        agent_version="test",
        prompt_version="test",
        retrieval_version="test",
    )
    admission = await runtime.runs.admit(
        subject="alice",
        conversation_id=conversation.id,
        request_key="turn",
        content="Question",
        identity=identity,
    )
    await runtime.runs.finish(
        subject="alice",
        run_id=admission.run.id,
        status=AttemptStatus.COMPLETED,
        content="Answer",
        sources=(),
        failure_category=None,
    )
    path = f"/v1/messages/{admission.run.assistant_message_id}/feedback"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer alice"},
    ) as client:
        assert (
            await client.put(path, json={"rating": "dislike", "run_id": str(uuid4())})
        ).status_code == 422
        assert (
            await client.put(path, json={"rating": "dislike", "comment": "Needs detail"})
        ).status_code == 204
        assert (await client.put(path, json={"rating": "like"})).status_code == 204
        history = (await client.get(f"/v1/conversations/{conversation.id}/messages")).json()[
            "messages"
        ]
        assert history[1]["run_id"] == str(admission.run.id)
        assert history[1]["trace_id"] == admission.run.trace_id
        assert history[1]["feedback"]["rating"] == "like"
        assert history[1]["feedback"]["comment"] is None
        client.headers["Authorization"] = "Bearer bob"
        assert (await client.delete(path)).status_code == 404
        client.headers["Authorization"] = "Bearer alice"
        assert (await client.delete(path)).status_code == 204
        assert (await client.get(f"/v1/conversations/{conversation.id}/messages")).json()[
            "messages"
        ][1]["feedback"] is None


async def test_nul_characters_are_rejected_before_any_state_change(
    runtime: ApiTestRuntime, chat_settings: Settings
) -> None:
    app = create_app(settings=chat_settings)
    app.dependency_overrides[get_chat_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer alice"},
    ) as client:
        conversation_id = (await client.post("/v1/conversations")).json()["id"]
        turn = await client.post(
            f"/v1/conversations/{conversation_id}/turns:stream",
            json={"content": "Horizon\x00budget"},
            headers={"Idempotency-Key": "nul-turn"},
        )
        assert turn.status_code == 422
        feedback = await client.put(
            f"/v1/conversations/{conversation_id}/feedback",
            json={"comment": "bad\x00comment"},
        )
        assert feedback.status_code == 422
    async with runtime.conversations.engine.connect() as conn:
        written = await conn.scalar(
            text(
                "SELECT (SELECT count(*) FROM app.turns) + (SELECT count(*) FROM app.messages)"
                " + (SELECT count(*) FROM app.agent_runs)"
                " + (SELECT count(*) FROM app.thread_feedback)"
            )
        )
    assert written == 0
