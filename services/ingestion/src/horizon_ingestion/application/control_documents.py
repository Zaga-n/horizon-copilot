"""Catalog of owner-filtered polling, bounded explicit retry and coordinated deletion."""

from uuid import UUID

from horizon_ingestion.domain.documents import Deletion, JobStatus
from horizon_ingestion.ports.indexing import StatusStore


async def poll_document(*, subject: str, document_id: UUID, store: StatusStore) -> JobStatus:
    return await store.document_status(subject=subject, document_id=document_id)


async def poll_job(*, subject: str, job_id: UUID, store: StatusStore) -> JobStatus:
    return await store.job_status(subject=subject, job_id=job_id)


async def list_documents(
    *, subject: str, limit: int, offset: int, store: StatusStore
) -> tuple[JobStatus, ...]:
    return await store.list_documents(subject=subject, limit=limit, offset=offset)


async def retry_job(*, subject: str, job_id: UUID, store: StatusStore) -> JobStatus:
    return await store.retry(subject=subject, job_id=job_id)


async def delete_document(*, subject: str, document_id: UUID, store: StatusStore) -> Deletion:
    await store.delete(subject=subject, document_id=document_id)
    result = await store.document_status(subject=subject, document_id=document_id)
    return Deletion(document_id=document_id, lifecycle=result.lifecycle)
