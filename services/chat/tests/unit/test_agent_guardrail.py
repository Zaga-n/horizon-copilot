"""Compiled agent scope guardrail: out-of-scope turns never reach the main model."""

import pytest

from horizon_chat.domain.agent import (
    AgentProgress,
    AnswerDelta,
    ProgressPhase,
)
from horizon_chat.domain.runs import CheckpointStart
from horizon_chat.genai.horizon_agent.schemas import FINAL_TAG, Scope
from horizon_chat_testing.agent import RecordingIndex, accepted, guardrail, runner
from horizon_chat_testing.models import ScriptedModel


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param(Scope.AMBIGUOUS, id="bare-project-name"),
        pytest.param(Scope.OUT_OF_SCOPE, id="unrelated"),
        pytest.param(Scope.INJECTION, id="prompt-injection"),
    ],
)
async def test_scope_outcomes_do_not_call_main_model_or_retrieval(scope: Scope) -> None:
    index = RecordingIndex(hits=())
    decision = ScriptedModel(script=[], disable_streaming=True)
    final = ScriptedModel(script=[], tags=[FINAL_TAG])
    utility = ScriptedModel(script=[guardrail(scope)], disable_streaming=True)
    agent = runner(decision=decision, final=final, utility=utility, index=index)
    admission = accepted()
    events = [
        event
        async for event in agent.answer(
            subject="alice",
            admission=admission,
            start=CheckpointStart(
                conversation_id=admission.run.conversation_id, checkpoint_id=None
            ),
        )
    ]
    assert len([event for event in events if isinstance(event, AnswerDelta)]) == 1
    assert index.subjects == []
    assert decision.seen == []
    assert final.seen == []
    assert [event.phase for event in events if isinstance(event, AgentProgress)] == [
        ProgressPhase.GUARDRAIL
    ]
