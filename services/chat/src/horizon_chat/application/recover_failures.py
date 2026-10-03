"""Replay sanitized intents without changing a completed or newer attempt."""

import logging
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from horizon_chat.domain.conversations import ConversationNotFoundError
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.ports.errors import DataIntegrityError
from horizon_chat.ports.runs import RunLedger

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryOutcome:
    repaired: int
    failed: int


async def recover_failures(
    *, buffer: RecoveryBuffer, store: RunLedger, conversation_id: UUID | None = None
) -> RecoveryOutcome:
    """Apply each pending intent; an inconsistent run is dropped so it cannot block the rest.

    An outage still raises and leaves the remaining intents queued.
    """
    repaired = failed = 0
    for intent in tuple(buffer.pending.values()):
        if conversation_id is not None and intent.conversation_id != conversation_id:
            continue
        try:
            with suppress(ConversationNotFoundError):
                await store.fail_pending(
                    subject=intent.subject, run_id=intent.run_id, failure_category=intent.category
                )
        except DataIntegrityError:
            # Durable lease expiry still fails this run once its lease lapses.
            logger.warning(
                "recovery_item_failed", extra={"run_id": str(intent.run_id)}, exc_info=True
            )
            failed += 1
        else:
            repaired += 1
        buffer.discard(intent)
    return RecoveryOutcome(repaired=repaired, failed=failed)
