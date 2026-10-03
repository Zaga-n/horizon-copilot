"""Turn streaming over HTTP: completion, replay, retry and feedback attribution."""

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from horizon_chat.api.dependencies import get_chat_runtime
from horizon_chat.application.submit_turn import Admitted, retry_turn
from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import Settings
from horizon_chat.domain.agent import (
    AgentProgress,
    ProgressPhase,
)
from horizon_chat.observability.tracing import Telemetry
from horizon_chat_testing.streaming import (
    ControlledAgent,
    StreamingRuntime,
    admit,
    events,
)

pytestmark = pytest.mark.integration


async def test_http_completion_replay_failure_and_explicit_retry(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
    chat_settings: Settings,
) -> None:
    runtime, agent, exporter = streaming
    agent.progress = AgentProgress(phase=ProgressPhase.GENERATION)
    app = create_app(settings=chat_settings)
    app.dependency_overrides[get_chat_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer alice"},
    ) as client:
        conversation = (await client.post("/v1/conversations")).json()["id"]
        path = f"/v1/conversations/{conversation}"
        request = {"content": "Question"}
        first = await client.post(
            path + "/turns:stream", json=request, headers={"Idempotency-Key": "one"}
        )
        emitted = events(first.text)
        assert [item["type"] for item in emitted] == [
            "started",
            "progress",
            "delta",
            "delta",
            "sources",
            "completed",
        ]
        progress_data = emitted[1]["data"]
        assert isinstance(progress_data, dict)
        assert progress_data["phase"] == "generation"
        message = progress_data["message"]
        assert isinstance(message, str)
        assert message.strip()
        assert "PRIVATE" not in first.text
        replay = await client.post(
            path + "/turns:stream", json=request, headers={"Idempotency-Key": "one"}
        )
        assert replay.json()["attempts"][0]["id"] == emitted[0]["run_id"]
        assert agent.calls == 1
        assert (
            await client.post(
                path + "/turns:stream",
                json={"content": "Different"},
                headers={"Idempotency-Key": "one"},
            )
        ).status_code == 409
        agent.fail = True
        failed = events(
            (
                await client.post(
                    path + "/turns:stream", json=request, headers={"Idempotency-Key": "two"}
                )
            ).text
        )
        assert failed[-1]["type"] == "failed"
        failed_data = failed[-1]["data"]
        assert isinstance(failed_data, dict) and failed_data["retry_available"] is True
        history = (await client.get(path + "/messages")).json()["messages"]
        assert history[-1]["status"] == "failed"
        assert history[-1]["content"] == "Visible prefix. "
        agent.fail = False
        retry_path = path + f"/turns/{failed[-1]['turn_id']}:retry"
        retry_body = {"expected_run_id": failed[-1]["run_id"]}
        retried = events(
            (
                await client.post(retry_path, json=retry_body, headers={"Idempotency-Key": "retry"})
            ).text
        )
        assert retried[-1]["type"] == "completed"
        assert retried[0]["trace_id"] != failed[0]["trace_id"]
        assert retried[0]["assistant_message_id"] != failed[0]["assistant_message_id"]
        again = await client.post(retry_path, json=retry_body, headers={"Idempotency-Key": "retry"})
        assert again.json()["attempts"][-1]["id"] == retried[0]["run_id"]
        assert agent.calls == 3
        assert (
            await client.post(retry_path, json=retry_body, headers={"Idempotency-Key": "stale"})
        ).status_code == 409
        history = (await client.get(path + "/messages")).json()["messages"]
        assert len([message for message in history if message["role"] == "user"]) == 2
    roots = [span for span in exporter.get_finished_spans() if span.name == "gen_ai.invoke_agent"]
    assert len(roots) == 3
    assert len({span.context.trace_id for span in roots if span.context}) == 3


@pytest.mark.parametrize("sampled", [True, False])
async def test_history_feedback_attribution_survives_sampling_and_export_outage(
    streaming: tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter],
    sampled: bool,
) -> None:
    from dataclasses import replace

    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON
    from sqlalchemy import text

    from horizon_chat.domain.feedback import FeedbackInput, Rating

    class BrokenExporter(SpanExporter):
        def export(self, spans: object) -> SpanExportResult:
            raise RuntimeError("PRIVATE export payload")

    runtime, agent, _ = streaming
    provider = TracerProvider(sampler=ALWAYS_ON if sampled else ALWAYS_OFF)
    provider.add_span_processor(SimpleSpanProcessor(BrokenExporter()))
    service = replace(
        runtime,
        telemetry=Telemetry(
            tracer=provider.get_tracer("test"), measurements=runtime.telemetry.measurements
        ),
    )
    try:
        conversation = await runtime.conversations.create(subject="alice")
        agent.fail = True
        failed, stream = await admit(
            service,
            subject="alice",
            conversation_id=conversation.id,
            request_key="first",
            content="Question",
        )
        assert [item async for item in stream][-1].event == "failed"
        agent.fail = False
        retry = await retry_turn(
            runs=service.runs,
            conversations=service.conversations,
            checkpoints=service.checkpoints,
            agent=service.agent,
            recovery=service.recovery,
            telemetry=service.telemetry,
            policy=service.turn_policy,
            subject="alice",
            conversation_id=conversation.id,
            turn_id=failed.run.turn_id,
            expected_run_id=failed.run.id,
            request_key="retry",
        )
        assert isinstance(retry, Admitted)
        retried = retry.admission
        assert [item async for item in retry.events][-1].event == "completed"
        assert retried.run.trace_id != failed.run.trace_id
        await runtime.feedback.put_answer_feedback(
            subject="alice",
            message_id=retried.run.assistant_message_id,
            request=FeedbackInput(rating=Rating.LIKE),
        )
        history = await runtime.conversations.history(
            subject="alice", conversation_id=conversation.id, cursor=0, limit=100
        )
        assert history.messages[-1].trace_id == retried.run.trace_id
        assert history.messages[-1].feedback is not None
        async with runtime.conversations.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.id AS run_id, r.trace_id FROM app.message_feedback f JOIN app.agent_runs r ON r.assistant_message_id=f.assistant_message_id"
                    )
                )
            ).one()
            assert row.run_id == retried.run.id and row.trace_id == retried.run.trace_id
    finally:
        provider.shutdown()
