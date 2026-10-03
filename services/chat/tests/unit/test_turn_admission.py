"""The admission action mints run and message ids; tracing only records them."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from horizon_chat.application.submit_turn import Admitted, submit_turn
from horizon_chat.domain.agent import AgentEvent
from horizon_chat.domain.conversations import Conversation, ConversationPage, HistoryPage
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import (
    Admission,
    AttemptStatus,
    CheckpointPlan,
    CheckpointStart,
    FailureCategory,
    Run,
    RunIdentity,
    Turn,
    TurnPolicy,
)
from horizon_chat.domain.sources import Source
from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry

RUN_ID = UUID("00000000-0000-0000-0000-00000000000a")
MESSAGE_ID = UUID("00000000-0000-0000-0000-00000000000b")


@dataclass(slots=True, kw_only=True)
class AdmittingLedger:
    """Admits every request; records the identity the action reserved."""

    identities: list[RunIdentity] = field(default_factory=list)

    async def admit(
        self,
        *,
        subject: str,
        conversation_id: UUID,
        request_key: str,
        content: str,
        identity: RunIdentity,
    ) -> Admission:
        self.identities.append(identity)
        run = Run(
            id=identity.id,
            conversation_id=conversation_id,
            turn_id=conversation_id,
            user_message_id=conversation_id,
            assistant_message_id=identity.assistant_message_id,
            attempt_number=1,
            trace_id=identity.trace_id,
            root_span_id=identity.root_span_id,
            status=AttemptStatus.PENDING,
            failure_category=None,
            retry_of_run_id=None,
            started_at=datetime.now(UTC),
            ended_at=None,
            agent_version=identity.agent_version,
            prompt_version=identity.prompt_version,
            retrieval_version=identity.retrieval_version,
        )
        return Admission(run=run, input=content, replayed=False)

    async def retry(self, **_: object) -> Admission:
        raise NotImplementedError

    async def save_partial(self, *, subject: str, run_id: UUID, content: str) -> None:
        raise NotImplementedError

    async def finish(
        self,
        *,
        subject: str,
        run_id: UUID,
        status: AttemptStatus,
        content: str,
        sources: tuple[Source, ...],
        failure_category: FailureCategory | None,
    ) -> None:
        raise NotImplementedError

    async def fail_pending(
        self, *, subject: str, run_id: UUID, failure_category: FailureCategory
    ) -> AttemptStatus:
        raise NotImplementedError

    async def checkpoint_plan(self, *, subject: str, run_id: UUID) -> CheckpointPlan:
        raise NotImplementedError

    async def record_checkpoint(
        self, *, subject: str, run_id: UUID, checkpoint_id: str | None
    ) -> None:
        raise NotImplementedError


class UnusedCheckpoints:
    async def current_id(self, *, conversation_id: UUID) -> str | None:
        raise NotImplementedError

    async def require_checkpoint(self, *, conversation_id: UUID, checkpoint_id: str) -> None:
        raise NotImplementedError

    async def delete_thread(self, *, conversation_id: UUID) -> None:
        raise NotImplementedError


class UnusedConversations:
    """A new admission never reads the stored turn."""

    async def create(self, *, subject: str) -> Conversation:
        raise NotImplementedError

    async def list(self, *, subject: str, cursor: UUID | None, limit: int) -> ConversationPage:
        raise NotImplementedError

    async def detail(self, *, subject: str, conversation_id: UUID) -> Conversation:
        raise NotImplementedError

    async def history(
        self, *, subject: str, conversation_id: UUID, cursor: int, limit: int
    ) -> HistoryPage:
        raise NotImplementedError

    async def turn(self, *, subject: str, conversation_id: UUID, turn_id: UUID) -> Turn:
        raise NotImplementedError


class UnusedAgent:
    def answer(
        self, *, subject: str, admission: Admission, start: CheckpointStart
    ) -> AsyncGenerator[AgentEvent]:
        raise NotImplementedError


async def test_admission_uses_the_injected_id_factory_and_versions() -> None:
    ids = iter((RUN_ID, MESSAGE_ID))
    ledger = AdmittingLedger()
    traces, meters = TracerProvider(), MeterProvider()
    submission = await submit_turn(
        runs=ledger,
        conversations=UnusedConversations(),
        checkpoints=UnusedCheckpoints(),
        agent=UnusedAgent(),
        recovery=RecoveryBuffer(),
        telemetry=Telemetry(
            tracer=traces.get_tracer("test"),
            measurements=Measurements(meter=meters.get_meter("test")),
        ),
        policy=TurnPolicy(
            agent_version="agent-test",
            prompt_version="prompt-test",
            retrieval_version="retrieval-test",
            persistence_attempts=1,
            persistence_backoff_seconds=0,
            persistence_max_backoff_seconds=0,
            cleanup_timeout_seconds=1,
        ),
        subject="alice",
        conversation_id=RUN_ID,
        request_key="key",
        content="Horizon",
        id_factory=lambda: next(ids),
    )
    assert isinstance(submission, Admitted)
    admission = submission.admission
    identity = ledger.identities[0]
    assert (identity.id, identity.assistant_message_id) == (RUN_ID, MESSAGE_ID)
    assert (identity.agent_version, identity.retrieval_version) == (
        "agent-test",
        "retrieval-test",
    )
    assert admission.run.id == RUN_ID
    traces.shutdown()
    meters.shutdown()
