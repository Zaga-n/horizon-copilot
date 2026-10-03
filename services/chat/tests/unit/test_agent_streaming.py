"""Final-answer streaming and checkpoint summarization in the compiled agent."""

from langchain_core.messages import AIMessage, HumanMessage

from horizon_chat.domain.agent import (
    AgentProgress,
    AnswerDelta,
    ProgressPhase,
)
from horizon_chat.domain.runs import CheckpointStart
from horizon_chat.genai.horizon_agent.middleware.budget import PhysicalAttemptBudget
from horizon_chat.genai.horizon_agent.runner import attempt_config
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG, Scope
from horizon_chat_testing.agent import RecordingIndex, accepted, guardrail, runner
from horizon_chat_testing.models import ScriptedModel


async def test_long_checkpoint_history_is_summarized_without_exposing_summary_as_answer() -> None:
    utility = ScriptedModel(
        script=[guardrail(Scope.IN_SCOPE), AIMessage(content="PRIVATE internal summary")],
        disable_streaming=True,
        callbacks=[PhysicalAttemptBudget()],
    )
    decision = ScriptedModel(script=[AIMessage(content="PRIVATE draft")], disable_streaming=True)
    final = ScriptedModel(script=[AIMessage(content="Horizon funding answer.")], tags=[FINAL_TAG])
    agent = runner(
        decision=decision,
        final=final,
        utility=utility,
        index=RecordingIndex(hits=()),
        summary_tokens=100,
        keep_messages=2,
    )
    admission = accepted()
    start = CheckpointStart(conversation_id=admission.run.conversation_id, checkpoint_id=None)
    messages = [
        message
        for _ in range(20)
        for message in (
            HumanMessage(content="Prior Horizon question " * 10),
            AIMessage(content="Prior Horizon answer " * 10),
        )
    ]
    await agent.graph.aupdate_state(attempt_config(start), {"messages": messages})
    events = [
        item async for item in agent.answer(subject="alice", admission=admission, start=start)
    ]
    assert len(utility.seen) == 2
    assert "PRIVATE internal summary" in str(decision.seen[0])
    assert (
        "".join(item.text for item in events if isinstance(item, AnswerDelta))
        == "Horizon funding answer."
    )


async def test_compiled_agent_emits_first_delta_while_provider_is_still_streaming() -> None:
    import asyncio

    gate = asyncio.Event()
    final = ScriptedModel(
        script=[AIMessage(content="Incremental Horizon answer.")],
        tags=[FINAL_TAG],
        stream_gate=gate,
    )
    agent = runner(
        decision=ScriptedModel(script=[AIMessage(content="PRIVATE draft")], disable_streaming=True),
        final=final,
        utility=ScriptedModel(script=[guardrail(Scope.IN_SCOPE)], disable_streaming=True),
        index=RecordingIndex(hits=()),
    )
    admission = accepted()
    stream = agent.answer(
        subject="alice",
        admission=admission,
        start=CheckpointStart(conversation_id=admission.run.conversation_id, checkpoint_id=None),
    )
    try:
        assert not isinstance(await anext(stream), AnswerDelta)
        async with asyncio.timeout(0.3):
            progress = await anext(stream)
            assert isinstance(progress, AgentProgress)
            assert progress.phase == ProgressPhase.GENERATION
            first = await anext(stream)
        assert isinstance(first, AnswerDelta) and first.text
        assert not gate.is_set()
        gate.set()
        remaining = [item async for item in stream]
        assert (
            first.text + "".join(item.text for item in remaining if isinstance(item, AnswerDelta))
            == "Incremental Horizon answer."
        )
    finally:
        gate.set()
        await stream.aclose()
