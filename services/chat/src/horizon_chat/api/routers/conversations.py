"""Owned conversation/history/reconciliation and feedback HTTP contracts."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from horizon_chat.api.dependencies import ChatRuntimeDep, SubjectDep
from horizon_chat.application import conversations as actions
from horizon_chat.domain.conversations import (
    MAX_PAGE_SIZE,
    Conversation,
    ConversationPage,
    HistoryPage,
)
from horizon_chat.domain.feedback import FeedbackInput
from horizon_chat.domain.runs import Turn

router = APIRouter(prefix="/v1")
type PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.post("/conversations", response_model=Conversation, status_code=status.HTTP_201_CREATED)
async def create(runtime: ChatRuntimeDep, subject: SubjectDep) -> Conversation:
    return await actions.create_conversation(subject=subject, store=runtime.conversations)


@router.get("/conversations", response_model=ConversationPage)
async def list_owned(
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
    cursor: UUID | None = None,
    limit: PageLimit = 50,
) -> ConversationPage:
    return await actions.list_conversations(
        subject=subject, cursor=cursor, limit=limit, store=runtime.conversations
    )


@router.get("/conversations/{conversation_id}", response_model=Conversation)
async def detail(
    conversation_id: UUID, runtime: ChatRuntimeDep, subject: SubjectDep
) -> Conversation:
    return await actions.conversation_detail(
        subject=subject, conversation_id=conversation_id, store=runtime.conversations
    )


@router.get("/conversations/{conversation_id}/messages", response_model=HistoryPage)
async def history(
    conversation_id: UUID,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: PageLimit = 50,
) -> HistoryPage:
    return await actions.message_history(
        subject=subject,
        conversation_id=conversation_id,
        cursor=cursor,
        limit=limit,
        store=runtime.conversations,
    )


@router.get("/conversations/{conversation_id}/turns/{turn_id}", response_model=Turn)
async def turn_status(
    conversation_id: UUID,
    turn_id: UUID,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
) -> Turn:
    return await actions.reconcile_turn(
        subject=subject,
        conversation_id=conversation_id,
        turn_id=turn_id,
        store=runtime.conversations,
        runs=runtime.runs,
        recovery=runtime.recovery,
    )


@router.put("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def answer_feedback(
    message_id: UUID,
    body: FeedbackInput,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
) -> Response:
    await actions.put_answer_feedback(
        subject=subject, message_id=message_id, request=body, store=runtime.feedback
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def clear_answer_feedback(
    message_id: UUID, runtime: ChatRuntimeDep, subject: SubjectDep
) -> Response:
    await actions.delete_answer_feedback(
        subject=subject, message_id=message_id, store=runtime.feedback
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/conversations/{conversation_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def thread_feedback(
    conversation_id: UUID,
    body: FeedbackInput,
    runtime: ChatRuntimeDep,
    subject: SubjectDep,
) -> Response:
    await actions.put_thread_feedback(
        subject=subject, conversation_id=conversation_id, request=body, store=runtime.feedback
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/conversations/{conversation_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def clear_thread_feedback(
    conversation_id: UUID, runtime: ChatRuntimeDep, subject: SubjectDep
) -> Response:
    await actions.delete_thread_feedback(
        subject=subject, conversation_id=conversation_id, store=runtime.feedback
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
