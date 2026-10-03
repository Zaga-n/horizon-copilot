"""Authenticated bounded multipart uploads and private lifecycle HTTP endpoints."""

import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from horizon_ingestion.api.dependencies import ApiRuntime, RuntimeDep
from horizon_ingestion.api.errors import UploadFormError, UploadTimeoutError
from horizon_ingestion.api.schemas import AcceptanceBody, DeletionBody, JobStatusBody
from horizon_ingestion.application.control_documents import (
    delete_document,
    list_documents,
    poll_document,
    poll_job,
    retry_job,
)
from horizon_ingestion.application.identity import authenticate
from horizon_ingestion.application.upload_document import upload_document
from horizon_ingestion.domain.acceptance import UploadMetadata
from horizon_ingestion.domain.documents import (
    JobState,
    Lifecycle,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")
MAX_METADATA_BYTES = 16384


async def subject(
    runtime: RuntimeDep, authorization: Annotated[str | None, Header()] = None
) -> str:
    token = (
        authorization.removeprefix("Bearer ")
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    return await authenticate(token=token, verifier=runtime.identity)


@router.post("/documents", response_model=AcceptanceBody)
async def upload(
    request: Request,
    response: Response,
    runtime: RuntimeDep,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    authorization: Annotated[str | None, Header()] = None,
) -> AcceptanceBody:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("upload"):
        return await _upload(
            request=request,
            response=response,
            runtime=runtime,
            idempotency_key=idempotency_key,
            caller=caller,
        )


def _metadata(*, metadata_text: str, document_id: object) -> UploadMetadata:
    """Decode the client's metadata fields; only their malformation is a 422."""
    try:
        values = UploadMetadata.model_validate_json(metadata_text)
    except ValidationError as exc:
        raise UploadFormError("invalid_upload_metadata") from exc
    if document_id is None:
        return values
    if not isinstance(document_id, str) or values.document_id is not None:
        raise UploadFormError("invalid_document_id")
    try:
        target = UUID(document_id)
    except ValueError as exc:
        raise UploadFormError("invalid_document_id") from exc
    return UploadMetadata(
        document_id=target, corpus=values.corpus, project_metadata=values.project_metadata
    )


async def _upload(
    *, request: Request, response: Response, runtime: ApiRuntime, idempotency_key: str, caller: str
) -> AcceptanceBody:
    try:
        async with asyncio.timeout(runtime.upload_timeout_seconds):
            async with request.form(
                max_files=1, max_fields=2, max_part_size=MAX_METADATA_BYTES
            ) as form:
                file = form.get("file")
                metadata_text = form.get("metadata", "{}")
                document_id = form.get("document_id")
                if not isinstance(file, UploadFile) or not isinstance(metadata_text, str):
                    raise UploadFormError("file_and_metadata_required")
                values = _metadata(metadata_text=metadata_text, document_id=document_id)
                result = await upload_document(
                    subject=caller,
                    key=idempotency_key,
                    stream=file,
                    filename=file.filename or "",
                    content_type=file.content_type or "",
                    metadata=values,
                    pipeline=runtime.pipeline,
                    preparer=runtime.preparer,
                    storage=runtime.storage,
                    store=runtime.acceptance,
                    timeout_seconds=runtime.upload_timeout_seconds,
                    trace_context=runtime.telemetry.carrier(),
                )
    except TimeoutError as exc:
        raise UploadTimeoutError() from exc
    response.status_code = (
        status.HTTP_202_ACCEPTED
        if result.status in (JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING)
        else status.HTTP_200_OK
    )
    body = AcceptanceBody.of(result)
    response.headers["Location"] = body.status_url
    log.info(
        "ingestion_upload_accepted",
        extra={
            "job_id": str(result.job_id),
            "document_id": str(result.document_id),
            "deduplicated": result.deduplicated,
        },
    )
    return body


@router.get("/documents", response_model=tuple[JobStatusBody, ...])
async def list_owned(
    runtime: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[JobStatusBody, ...]:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("status"):
        listed = await list_documents(
            subject=caller, limit=limit, offset=offset, store=runtime.status
        )
    return tuple(JobStatusBody.of(item) for item in listed)


@router.get("/documents/{document_id}", response_model=JobStatusBody)
async def document(
    document_id: UUID, runtime: RuntimeDep, authorization: Annotated[str | None, Header()] = None
) -> JobStatusBody:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("status"):
        result = await poll_document(subject=caller, document_id=document_id, store=runtime.status)
    return JobStatusBody.of(result)


@router.get("/ingestion-jobs/{job_id}", response_model=JobStatusBody)
async def job(
    job_id: UUID, runtime: RuntimeDep, authorization: Annotated[str | None, Header()] = None
) -> JobStatusBody:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("status"):
        result = await poll_job(subject=caller, job_id=job_id, store=runtime.status)
    return JobStatusBody.of(result)


@router.post("/ingestion-jobs/{job_id}:retry", response_model=JobStatusBody)
async def retry(
    job_id: UUID,
    runtime: RuntimeDep,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> JobStatusBody:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("retry"):
        result = await retry_job(subject=caller, job_id=job_id, store=runtime.status)
    response.status_code = (
        status.HTTP_200_OK if result.status == JobState.READY else status.HTTP_202_ACCEPTED
    )
    return JobStatusBody.of(result)


@router.delete("/documents/{document_id}", response_model=DeletionBody)
async def remove(
    document_id: UUID,
    runtime: RuntimeDep,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
) -> DeletionBody:
    caller = await subject(runtime, authorization)
    with runtime.telemetry.work("delete"):
        result = await delete_document(
            subject=caller, document_id=document_id, store=runtime.status
        )
    response.status_code = (
        status.HTTP_200_OK if result.lifecycle == Lifecycle.DELETED else status.HTTP_202_ACCEPTED
    )
    return DeletionBody.of(result)
