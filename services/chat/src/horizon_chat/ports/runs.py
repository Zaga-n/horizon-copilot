"""Run admission, completion, and checkpoint bookkeeping for one conversation at a time."""

from typing import Protocol
from uuid import UUID

from horizon_chat.domain.runs import (
    Admission,
    AttemptStatus,
    CheckpointPlan,
    FailureCategory,
    RunIdentity,
)
from horizon_chat.domain.sources import Source


class RunLedger(Protocol):
    """Raises the conversation ledger's errors (ports/conversations.py)."""

    async def admit(
        self,
        *,
        subject: str,
        conversation_id: UUID,
        request_key: str,
        content: str,
        identity: RunIdentity,
    ) -> Admission: ...
    async def retry(
        self,
        *,
        subject: str,
        conversation_id: UUID,
        turn_id: UUID,
        expected_run_id: UUID,
        request_key: str,
        identity: RunIdentity,
    ) -> Admission: ...
    async def save_partial(self, *, subject: str, run_id: UUID, content: str) -> None: ...
    async def finish(
        self,
        *,
        subject: str,
        run_id: UUID,
        status: AttemptStatus,
        content: str,
        sources: tuple[Source, ...],
        failure_category: FailureCategory | None,
    ) -> None: ...
    async def fail_pending(
        self, *, subject: str, run_id: UUID, failure_category: FailureCategory
    ) -> AttemptStatus: ...
    async def checkpoint_plan(self, *, subject: str, run_id: UUID) -> CheckpointPlan: ...
    async def record_checkpoint(
        self, *, subject: str, run_id: UUID, checkpoint_id: str | None
    ) -> None: ...
