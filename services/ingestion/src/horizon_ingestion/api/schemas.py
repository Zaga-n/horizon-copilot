"""HTTP response bodies: domain results plus the resource URLs only the HTTP layer knows."""

from typing import Self
from uuid import UUID

from horizon_ingestion.domain.documents import Acceptance, Deletion, JobStatus


def job_path(job_id: UUID) -> str:
    return f"/v1/ingestion-jobs/{job_id}"


def retry_path(job_id: UUID) -> str:
    return f"/v1/ingestion-jobs/{job_id}:retry"


def document_path(document_id: UUID) -> str:
    return f"/v1/documents/{document_id}"


class JobStatusBody(JobStatus):
    retry_url: str | None
    status_url: str

    @classmethod
    def of(cls, status: JobStatus) -> Self:
        return cls(
            **status.model_dump(),
            retry_url=retry_path(status.job_id) if status.retry_available else None,
            status_url=job_path(status.job_id),
        )


class AcceptanceBody(Acceptance):
    status_url: str
    retry_url: str | None

    @classmethod
    def of(cls, acceptance: Acceptance) -> Self:
        return cls(
            **acceptance.model_dump(),
            status_url=job_path(acceptance.job_id),
            retry_url=retry_path(acceptance.job_id) if acceptance.retry_available else None,
        )


class DeletionBody(Deletion):
    status_url: str

    @classmethod
    def of(cls, deletion: Deletion) -> Self:
        return cls(**deletion.model_dump(), status_url=document_path(deletion.document_id))
