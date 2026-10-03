"""One retention pass once migrations are ready; the supervisor owns the cadence."""

from typing import Protocol


class MaintenanceReadiness(Protocol):
    """Maintenance waits for deployment migrations without running them itself."""

    async def check(self) -> bool: ...


class RetentionBatch(Protocol):
    """The result of one purged batch."""

    @property
    def more_due(self) -> bool: ...


class RetentionJob(Protocol):
    """Implemented in db/, which workers/ may not import."""

    async def run_once(self) -> RetentionBatch: ...


async def retention_iteration(*, job: RetentionJob, readiness: MaintenanceReadiness) -> bool:
    """Purge one batch; True when the next batch is already due.

    Per-conversation failures are isolated inside the job; an outage raises.
    """
    if not await readiness.check():
        return False
    return (await job.run_once()).more_due
