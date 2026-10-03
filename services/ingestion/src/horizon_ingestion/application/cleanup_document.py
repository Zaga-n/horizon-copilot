"""Remove a deleted document's or superseded version's originals, then tombstone its rows."""

from horizon_ingestion.application.job_context import JobContext
from horizon_ingestion.ports.indexing import Claim


async def cleanup_document(*, claim: Claim, context: JobContext) -> None:
    with context.telemetry.work("cleanup"):
        for ref in await context.cleanup.cleanup_refs(claim=claim):
            await context.storage.remove(ref=ref)
        await context.cleanup.finish_cleanup(claim=claim)
