"""The run ledger: admission, attempt progress and outcome, and checkpoint bookkeeping.

Each method is one short transaction under the conversation row lock, never across model I/O.
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_chat.db.ledger import (
    database_now,
    end_attempt,
    held_run,
    owned_conversation,
    owned_run,
    recover_expired_run,
    reserve_run,
    run_from_row,
)
from horizon_chat.db.tables import MESSAGES, RETRIES, RUNS, TURNS
from horizon_chat.db.transactions import transaction
from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.runs import (
    ADMISSION,
    Admission,
    AttemptStatus,
    CheckpointPlan,
    FailureCategory,
    IdempotencyConflictError,
    MessageRole,
    RunIdentity,
    ensure_idle,
    ensure_retryable,
    ensure_same_input,
    input_hash,
    terminal_transition,
)
from horizon_chat.domain.sources import Source
from horizon_chat.ports.conversations import ConversationStoreIntegrityError


@dataclass(frozen=True, slots=True, kw_only=True)
class SqlRunLedger:
    """Serializes transitions with conversation locks, without locks across model I/O."""

    engine: AsyncEngine
    turn_lease_seconds: float
    id_factory: Callable[[], UUID] = uuid4

    async def admit(
        self,
        *,
        subject: str,
        conversation_id: UUID,
        request_key: str,
        content: str,
        identity: RunIdentity,
    ) -> Admission:
        async with transaction(self.engine) as conn:
            conversation = await owned_conversation(
                conn, subject=subject, conversation_id=conversation_id
            )
            existing = (
                (
                    await conn.execute(
                        select(TURNS).where(
                            TURNS.c.conversation_id == conversation_id,
                            TURNS.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            now = await database_now(conn)
            await recover_expired_run(conn, conversation, now)
            if existing is not None:
                ensure_same_input(stored_hash=existing["input_hash"], content=content)
                existing_run = (
                    (
                        await conn.execute(
                            select(RUNS)
                            .where(RUNS.c.turn_id == existing["id"])
                            .order_by(RUNS.c.attempt_number.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one()
                )
                return Admission(run=run_from_row(existing_run), input=content, replayed=True)
            ensure_idle(lease_until=conversation["lease_until"], now=now)
            turn_id, user_message_id = self.id_factory(), self.id_factory()
            order = conversation["next_message_order"]
            await conn.execute(
                insert(TURNS).values(
                    id=turn_id,
                    conversation_id=conversation_id,
                    user_id=conversation["user_id"],
                    request_key=request_key,
                    input_hash=input_hash(content),
                    user_message_id=user_message_id,
                    status=ADMISSION.turn,
                )
            )
            await conn.execute(
                insert(MESSAGES).values(
                    id=user_message_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    attempt_number=0,
                    role=MessageRole.USER,
                    content=content,
                    status=ADMISSION.user_message,
                    message_order=order,
                )
            )
            run = await reserve_run(
                conn,
                turn_lease_seconds=self.turn_lease_seconds,
                conversation=conversation,
                turn_id=turn_id,
                user_message_id=user_message_id,
                identity=identity,
                attempt_number=1,
                retry_of_run_id=None,
                message_order=order + 1,
                now=now,
            )
            return Admission(run=run, input=content, replayed=False)

    async def retry(
        self,
        *,
        subject: str,
        conversation_id: UUID,
        turn_id: UUID,
        expected_run_id: UUID,
        request_key: str,
        identity: RunIdentity,
    ) -> Admission:
        async with transaction(self.engine) as conn:
            conversation = await owned_conversation(
                conn, subject=subject, conversation_id=conversation_id
            )
            now = await database_now(conn)
            await recover_expired_run(conn, conversation, now)
            replay = (
                (
                    await conn.execute(
                        select(RETRIES).where(
                            RETRIES.c.conversation_id == conversation_id,
                            RETRIES.c.request_key == request_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                if replay["turn_id"] != turn_id or replay["expected_run_id"] != expected_run_id:
                    raise IdempotencyConflictError()
                row = (
                    (await conn.execute(select(RUNS).where(RUNS.c.id == replay["assigned_run_id"])))
                    .mappings()
                    .one()
                )
                run = run_from_row(row)
            else:
                ensure_idle(lease_until=conversation["lease_until"], now=now)
                latest = (
                    (
                        await conn.execute(
                            select(RUNS)
                            .join(MESSAGES, RUNS.c.assistant_message_id == MESSAGES.c.id)
                            .where(RUNS.c.conversation_id == conversation_id)
                            .order_by(MESSAGES.c.message_order.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if latest is None:
                    raise ConversationNotFoundError()
                previous = run_from_row(latest)
                ensure_retryable(latest=previous, expected_run_id=expected_run_id, turn_id=turn_id)
                run = await reserve_run(
                    conn,
                    turn_lease_seconds=self.turn_lease_seconds,
                    conversation=conversation,
                    turn_id=turn_id,
                    user_message_id=previous.user_message_id,
                    identity=identity,
                    attempt_number=previous.attempt_number + 1,
                    retry_of_run_id=previous.id,
                    message_order=conversation["next_message_order"],
                    now=now,
                )
                await conn.execute(
                    update(TURNS)
                    .where(TURNS.c.id == turn_id)
                    .values(status=ADMISSION.turn, updated_at=now)
                )
                await conn.execute(
                    insert(RETRIES).values(
                        user_id=conversation["user_id"],
                        conversation_id=conversation_id,
                        request_key=request_key,
                        turn_id=turn_id,
                        expected_run_id=expected_run_id,
                        assigned_run_id=run.id,
                    )
                )
            content = await conn.scalar(
                select(MESSAGES.c.content).where(MESSAGES.c.id == run.user_message_id)
            )
            if not isinstance(content, str):
                raise ConversationStoreIntegrityError("missing_user_input")
            return Admission(run=run, input=content, replayed=replay is not None)

    async def save_partial(self, *, subject: str, run_id: UUID, content: str) -> None:
        async with transaction(self.engine) as conn:
            _, run, now = await held_run(conn, subject=subject, run_id=run_id)
            await conn.execute(
                update(MESSAGES)
                .where(
                    MESSAGES.c.id == run["assistant_message_id"],
                    MESSAGES.c.status == AttemptStatus.PENDING,
                )
                .values(content=content, updated_at=now)
            )

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
        transition = terminal_transition(status=status, failure_category=failure_category)
        async with transaction(self.engine) as conn:
            _, run, now = await held_run(conn, subject=subject, run_id=run_id)
            await end_attempt(
                conn, run=run, transition=transition, now=now, content=content, sources=sources
            )

    async def fail_pending(
        self, *, subject: str, run_id: UUID, failure_category: FailureCategory
    ) -> AttemptStatus:
        """Fail the attempt if still pending; returns the attempt's status after the call.

        Only the reserved active attempt is reconciled; committed text and outcomes are kept.
        """
        transition = terminal_transition(
            status=AttemptStatus.FAILED, failure_category=failure_category
        )
        async with transaction(self.engine) as conn:
            """Reconcile only the reserved active attempt, preserving committed text/outcomes."""
            conversation, run = await owned_run(conn, subject=subject, run_id=run_id)
            if run["status"] != AttemptStatus.PENDING:
                return AttemptStatus(run["status"])
            if conversation["active_run_id"] != run_id:
                raise ConversationStoreIntegrityError("pending_run_not_active")
            await end_attempt(conn, run=run, transition=transition, now=await database_now(conn))
            return transition.attempt

    async def checkpoint_plan(self, *, subject: str, run_id: UUID) -> CheckpointPlan:
        async with transaction(self.engine) as conn:
            conversation, row, _ = await held_run(conn, subject=subject, run_id=run_id)
            base = row["base_checkpoint_id"]
            predecessor_prepared = False
            if row["retry_of_run_id"] is not None:
                previous = (
                    (await conn.execute(select(RUNS).where(RUNS.c.id == row["retry_of_run_id"])))
                    .mappings()
                    .one()
                )
                predecessor_prepared = previous["checkpoint_prepared"]
                if not row["checkpoint_prepared"]:
                    base = previous["base_checkpoint_id"]
            return CheckpointPlan(
                conversation_id=conversation["id"],
                prepared=row["checkpoint_prepared"],
                base_checkpoint_id=base,
                retrying=row["retry_of_run_id"] is not None,
                predecessor_prepared=predecessor_prepared,
            )

    async def record_checkpoint(
        self, *, subject: str, run_id: UUID, checkpoint_id: str | None
    ) -> None:
        async with transaction(self.engine) as conn:
            await held_run(conn, subject=subject, run_id=run_id)
            await conn.execute(
                update(RUNS)
                .where(RUNS.c.id == run_id, RUNS.c.checkpoint_prepared.is_(False))
                .values(base_checkpoint_id=checkpoint_id, checkpoint_prepared=True)
            )
