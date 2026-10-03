"""Runtime checkpoint access; no schema setup or migration commands."""

from dataclasses import dataclass
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import DatabaseError, InterfaceError, OperationalError
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import PoolTimeout

from horizon_chat.ports.checkpoints import (
    CheckpointStoreIntegrityError,
    CheckpointStoreUnavailableError,
)

# A revoked privilege fails every thread alike, so it waits for an operator like an outage.
UNAVAILABLE = (OperationalError, InterfaceError, PoolTimeout, InsufficientPrivilege)


@dataclass(frozen=True, slots=True, kw_only=True)
class PostgresCheckpointStore:
    """Use the library's public thread APIs, retaining schema ownership in LangGraph."""

    saver: AsyncPostgresSaver

    async def current_id(self, *, conversation_id: UUID) -> str | None:
        try:
            state = await self.saver.aget_tuple(
                {"configurable": {"thread_id": str(conversation_id)}}
            )
        except UNAVAILABLE as exc:
            raise CheckpointStoreUnavailableError("checkpoint_unavailable") from exc
        except DatabaseError as exc:
            raise CheckpointStoreIntegrityError("checkpoint_database_error") from exc
        return state.checkpoint["id"] if state is not None else None

    async def require_checkpoint(self, *, conversation_id: UUID, checkpoint_id: str) -> None:
        try:
            state = await self.saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": str(conversation_id),
                        "checkpoint_id": checkpoint_id,
                    }
                }
            )
        except UNAVAILABLE as exc:
            raise CheckpointStoreUnavailableError("checkpoint_unavailable") from exc
        except DatabaseError as exc:
            raise CheckpointStoreIntegrityError("checkpoint_database_error") from exc
        if state is None:
            raise CheckpointStoreIntegrityError("checkpoint_missing")

    async def delete_thread(self, *, conversation_id: UUID) -> None:
        try:
            await self.saver.adelete_thread(str(conversation_id))
        except UNAVAILABLE as exc:
            raise CheckpointStoreUnavailableError("checkpoint_unavailable") from exc
        except DatabaseError as exc:
            raise CheckpointStoreIntegrityError("checkpoint_database_error") from exc
