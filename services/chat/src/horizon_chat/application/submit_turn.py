"""Admit a new turn or a retry and stream it; a replayed request returns the stored turn."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from horizon_chat.application._turn_stream import stream_attempt
from horizon_chat.application.recover_failures import recover_failures
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import Admission, Turn, TurnPolicy
from horizon_chat.domain.streaming import StreamEvent
from horizon_chat.observability.tracing import Telemetry, TurnObservation
from horizon_chat.ports.agent import HorizonAgent
from horizon_chat.ports.checkpoints import CheckpointStore
from horizon_chat.ports.conversations import ConversationStore
from horizon_chat.ports.runs import RunLedger


@dataclass(frozen=True, slots=True, kw_only=True)
class Replayed:
    """The idempotency key was already admitted; nothing new runs."""

    turn: Turn


@dataclass(frozen=True, slots=True, kw_only=True)
class Admitted:
    """A new attempt holds the conversation lease; iterating `events` runs and settles it."""

    admission: Admission
    events: AsyncIterator[StreamEvent]


type Submission = Replayed | Admitted


def _reserve(
    *, telemetry: Telemetry, policy: TurnPolicy, id_factory: Callable[[], UUID]
) -> TurnObservation:
    return telemetry.reserve(
        run_id=id_factory(),
        assistant_message_id=id_factory(),
        agent_version=policy.agent_version,
        prompt_version=policy.prompt_version,
        retrieval_version=policy.retrieval_version,
    )


async def _settle(
    *,
    runs: RunLedger,
    conversations: ConversationStore,
    checkpoints: CheckpointStore,
    agent: HorizonAgent,
    recovery: RecoveryBuffer,
    policy: TurnPolicy,
    observation: TurnObservation,
    subject: str,
    admission: Admission,
) -> Submission:
    if admission.replayed:
        observation.discard()
        turn = await conversations.turn(
            subject=subject,
            conversation_id=admission.run.conversation_id,
            turn_id=admission.run.turn_id,
        )
        return Replayed(turn=turn)
    observation.admitted(admission.run)
    return Admitted(
        admission=admission,
        events=stream_attempt(
            runs=runs,
            checkpoints=checkpoints,
            agent=agent,
            recovery=recovery,
            policy=policy,
            observation=observation,
            subject=subject,
            admission=admission,
        ),
    )


async def submit_turn(
    *,
    runs: RunLedger,
    conversations: ConversationStore,
    checkpoints: CheckpointStore,
    agent: HorizonAgent,
    recovery: RecoveryBuffer,
    telemetry: Telemetry,
    policy: TurnPolicy,
    subject: str,
    conversation_id: UUID,
    request_key: str,
    content: str,
    id_factory: Callable[[], UUID] = uuid4,
) -> Submission:
    await recover_failures(buffer=recovery, store=runs, conversation_id=conversation_id)
    observation = _reserve(telemetry=telemetry, policy=policy, id_factory=id_factory)
    try:
        with observation.active():
            admission = await runs.admit(
                subject=subject,
                conversation_id=conversation_id,
                request_key=request_key,
                content=content,
                identity=observation.identity,
            )
    except BaseException as exc:
        observation.abandoned(exc)
        raise
    return await _settle(
        runs=runs,
        conversations=conversations,
        checkpoints=checkpoints,
        agent=agent,
        recovery=recovery,
        policy=policy,
        observation=observation,
        subject=subject,
        admission=admission,
    )


async def retry_turn(
    *,
    runs: RunLedger,
    conversations: ConversationStore,
    checkpoints: CheckpointStore,
    agent: HorizonAgent,
    recovery: RecoveryBuffer,
    telemetry: Telemetry,
    policy: TurnPolicy,
    subject: str,
    conversation_id: UUID,
    turn_id: UUID,
    expected_run_id: UUID,
    request_key: str,
    id_factory: Callable[[], UUID] = uuid4,
) -> Submission:
    await recover_failures(buffer=recovery, store=runs, conversation_id=conversation_id)
    observation = _reserve(telemetry=telemetry, policy=policy, id_factory=id_factory)
    try:
        with observation.active():
            admission = await runs.retry(
                subject=subject,
                conversation_id=conversation_id,
                turn_id=turn_id,
                expected_run_id=expected_run_id,
                request_key=request_key,
                identity=observation.identity,
            )
    except BaseException as exc:
        observation.abandoned(exc)
        raise
    return await _settle(
        runs=runs,
        conversations=conversations,
        checkpoints=checkpoints,
        agent=agent,
        recovery=recovery,
        policy=policy,
        observation=observation,
        subject=subject,
        admission=admission,
    )
