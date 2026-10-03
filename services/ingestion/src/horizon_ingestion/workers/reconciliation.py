"""One orphan-reconciliation pass; the supervisor owns the cadence."""

from typing import Protocol

from horizon_ingestion.observability.tracing import Telemetry


class ReconciliationJob(Protocol):
    """Implemented in db/, which workers/ may not import."""

    async def run_once(self) -> int: ...


async def reconciliation_iteration(*, job: ReconciliationJob, telemetry: Telemetry) -> None:
    """An outage raises to the supervisor, which backs off."""
    with telemetry.work("reconciliation"):
        removed = await job.run_once()
    telemetry.measurements.orphans.add(removed)
