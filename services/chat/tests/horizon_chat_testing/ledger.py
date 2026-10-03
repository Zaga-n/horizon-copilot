"""The three conversation-ledger stores over one engine, as bootstrap builds them."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_chat.db.conversations import SqlConversationStore
from horizon_chat.db.feedback import SqlFeedbackStore
from horizon_chat.db.runs import SqlRunLedger


@dataclass(frozen=True, slots=True, kw_only=True)
class Ledger:
    engine: AsyncEngine
    conversations: SqlConversationStore
    runs: SqlRunLedger
    feedback: SqlFeedbackStore


def ledger(engine: AsyncEngine) -> Ledger:
    return Ledger(
        engine=engine,
        conversations=SqlConversationStore(engine=engine),
        runs=SqlRunLedger(engine=engine, turn_lease_seconds=150),
        feedback=SqlFeedbackStore(engine=engine),
    )
