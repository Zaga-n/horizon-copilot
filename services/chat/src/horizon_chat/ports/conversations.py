"""Owned conversation reads and creation, plus the ledger's shared failure contract."""

from typing import Protocol
from uuid import UUID

from horizon_chat.domain.conversations import Conversation, ConversationPage, HistoryPage
from horizon_chat.domain.runs import Turn
from horizon_chat.ports.errors import DataIntegrityError, DependencyUnavailableError, RejectedError


class ConversationStoreUnavailableError(DependencyUnavailableError):
    """A transaction could not reach PostgreSQL, waited out the pool, or timed out."""


class ConversationStoreIntegrityError(DataIntegrityError):
    """Stored records or an unexpected database constraint are inconsistent."""


class ConversationStoreRejectedError(RejectedError):
    """PostgreSQL refused a value of this request (for example, text it cannot store)."""


class ConversationStore(Protocol):
    """All operations authorize the subject and reject purging conversations."""

    async def create(self, *, subject: str) -> Conversation: ...
    async def list(self, *, subject: str, cursor: UUID | None, limit: int) -> ConversationPage: ...
    async def detail(self, *, subject: str, conversation_id: UUID) -> Conversation: ...
    async def history(
        self, *, subject: str, conversation_id: UUID, cursor: int, limit: int
    ) -> HistoryPage: ...
    async def turn(self, *, subject: str, conversation_id: UUID, turn_id: UUID) -> Turn: ...
