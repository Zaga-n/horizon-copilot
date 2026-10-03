"""Checkpoint continuity and deletion capability without graph SDK types."""

from typing import Protocol
from uuid import UUID

from horizon_chat.ports.errors import DataIntegrityError, DependencyUnavailableError


class CheckpointStoreUnavailableError(DependencyUnavailableError):
    """The checkpoint database could not complete an operation."""


class CheckpointStoreIntegrityError(DataIntegrityError):
    """An attributed base checkpoint is missing or malformed."""


class CheckpointStore(Protocol):
    """Called only after owner/lease authorization or durable purge fencing."""

    async def current_id(self, *, conversation_id: UUID) -> str | None:
        """None means this conversation has no stored checkpoint."""
        ...

    async def require_checkpoint(self, *, conversation_id: UUID, checkpoint_id: str) -> None: ...
    async def delete_thread(self, *, conversation_id: UUID) -> None: ...
