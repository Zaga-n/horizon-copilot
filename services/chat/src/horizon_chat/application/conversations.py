"""Owner-authorized conversation, history, reconciliation, and feedback actions."""

from uuid import UUID

from horizon_chat.application.recover_failures import recover_failures
from horizon_chat.domain.conversations import Conversation, ConversationPage, HistoryPage
from horizon_chat.domain.feedback import FeedbackInput
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import Turn
from horizon_chat.ports.conversations import ConversationStore
from horizon_chat.ports.feedback import FeedbackStore
from horizon_chat.ports.identity import IdentityVerifier
from horizon_chat.ports.runs import RunLedger


async def authenticate(*, token: str | None, verifier: IdentityVerifier) -> str:
    return await verifier.verify(token=token)


async def create_conversation(*, subject: str, store: ConversationStore) -> Conversation:
    return await store.create(subject=subject)


async def list_conversations(
    *,
    subject: str,
    cursor: UUID | None,
    limit: int,
    store: ConversationStore,
) -> ConversationPage:
    return await store.list(subject=subject, cursor=cursor, limit=limit)


async def conversation_detail(
    *,
    subject: str,
    conversation_id: UUID,
    store: ConversationStore,
) -> Conversation:
    return await store.detail(subject=subject, conversation_id=conversation_id)


async def message_history(
    *,
    subject: str,
    conversation_id: UUID,
    cursor: int,
    limit: int,
    store: ConversationStore,
) -> HistoryPage:
    return await store.history(
        subject=subject, conversation_id=conversation_id, cursor=cursor, limit=limit
    )


async def reconcile_turn(
    *,
    subject: str,
    conversation_id: UUID,
    turn_id: UUID,
    store: ConversationStore,
    runs: RunLedger,
    recovery: RecoveryBuffer | None = None,
) -> Turn:
    if recovery is not None:
        await recover_failures(buffer=recovery, store=runs, conversation_id=conversation_id)
    return await store.turn(subject=subject, conversation_id=conversation_id, turn_id=turn_id)


async def put_answer_feedback(
    *,
    subject: str,
    message_id: UUID,
    request: FeedbackInput,
    store: FeedbackStore,
) -> None:
    await store.put_answer_feedback(subject=subject, message_id=message_id, request=request)


async def delete_answer_feedback(
    *,
    subject: str,
    message_id: UUID,
    store: FeedbackStore,
) -> None:
    await store.delete_answer_feedback(subject=subject, message_id=message_id)


async def put_thread_feedback(
    *,
    subject: str,
    conversation_id: UUID,
    request: FeedbackInput,
    store: FeedbackStore,
) -> None:
    await store.put_thread_feedback(
        subject=subject, conversation_id=conversation_id, request=request
    )


async def delete_thread_feedback(
    *,
    subject: str,
    conversation_id: UUID,
    store: FeedbackStore,
) -> None:
    await store.delete_thread_feedback(subject=subject, conversation_id=conversation_id)
