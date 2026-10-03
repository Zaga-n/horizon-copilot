"""Bounded volatile failure intents; durable lease recovery covers process loss."""

from dataclasses import dataclass, field
from uuid import UUID

from horizon_chat.domain.runs import FailureCategory


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureIntent:
    subject: str
    conversation_id: UUID
    run_id: UUID
    category: FailureCategory


@dataclass(slots=True, kw_only=True)
class RecoveryBuffer:
    capacity: int = 1024
    pending: dict[UUID, FailureIntent] = field(default_factory=dict)

    def add(self, intent: FailureIntent) -> None:
        if len(self.pending) >= self.capacity and intent.run_id not in self.pending:
            self.pending.pop(next(iter(self.pending)))
        self.pending[intent.run_id] = intent

    def discard(self, intent: FailureIntent) -> None:
        if self.pending.get(intent.run_id) == intent:
            del self.pending[intent.run_id]
