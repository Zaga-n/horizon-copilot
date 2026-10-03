"""Turn submission and retry routes; admission and streaming belong to the turn actions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from horizon_chat.api.dependencies import ChatRuntimeDep, SubjectDep
from horizon_chat.api.sse import encode_events
from horizon_chat.application.submit_turn import Replayed, Submission, retry_turn, submit_turn
from horizon_chat.domain.runs import Turn
from horizon_chat.domain.streaming import RetryInput, TurnInput

router = APIRouter(prefix="/v1")
type RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def response_for(submission: Submission) -> StreamingResponse | Turn:
    if isinstance(submission, Replayed):
        return submission.turn
    return StreamingResponse(
        encode_events(submission.events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/turns:stream", response_model=None)
async def submit(
    conversation_id: UUID,
    request: TurnInput,
    request_key: RequestKey,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
) -> StreamingResponse | Turn:
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
        content=request.content,
    )
    return response_for(submission)


@router.post("/conversations/{conversation_id}/turns/{turn_id}:retry", response_model=None)
async def retry(
    conversation_id: UUID,
    turn_id: UUID,
    request: RetryInput,
    request_key: RequestKey,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
) -> StreamingResponse | Turn:
    submission = await retry_turn(
        runs=runtime.runs,
        conversations=runtime.conversations,
        checkpoints=runtime.checkpoints,
        agent=runtime.agent,
        recovery=runtime.recovery,
        telemetry=runtime.telemetry,
        policy=runtime.turn_policy,
        subject=subject,
        conversation_id=conversation_id,
        turn_id=turn_id,
        expected_run_id=request.expected_run_id,
        request_key=request_key,
    )
    return response_for(submission)
