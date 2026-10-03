"""One reconciliation pass over bounded failure intents; the supervisor owns the cadence."""

import logging

from horizon_chat.application.recover_failures import recover_failures
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.runs import RunLedger

logger = logging.getLogger(__name__)


async def recovery_iteration(
    *, buffer: RecoveryBuffer, store: RunLedger, telemetry: Telemetry
) -> bool:
    """An outage raises to the supervisor and leaves the intents queued.

    Always False: one pass drains the buffer, so nothing more is due until new intents arrive.
    """
    outcome = await recover_failures(buffer=buffer, store=store)
    telemetry.measurements.recovery_failures.add(outcome.failed)
    if outcome.repaired or outcome.failed:
        logger.info(
            "recovery_completed",
            extra={"repaired_count": outcome.repaired, "failed_count": outcome.failed},
        )
    return False
