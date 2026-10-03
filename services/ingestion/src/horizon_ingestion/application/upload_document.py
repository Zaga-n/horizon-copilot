"""Validate, deduplicate and store an original before acknowledging a committed indexing job."""

from horizon_ingestion.domain.acceptance import UploadMetadata
from horizon_ingestion.domain.documents import Acceptance, Pipeline
from horizon_ingestion.ports.uploads import (
    AcceptanceStore,
    ObjectStorage,
    UploadPreparer,
    UploadStream,
    UploadValidationError,
)


async def upload_document(
    *,
    subject: str,
    key: str,
    stream: UploadStream,
    filename: str,
    content_type: str,
    metadata: UploadMetadata,
    pipeline: Pipeline,
    preparer: UploadPreparer,
    storage: ObjectStorage,
    store: AcceptanceStore,
    timeout_seconds: float,
    trace_context: dict[str, str],
) -> Acceptance:
    if not 1 <= len(key) <= 200:
        raise UploadValidationError("idempotency_key")
    async with preparer.prepare(
        stream=stream, filename=filename, content_type=content_type
    ) as upload:
        existing = await store.find_existing(
            subject=subject, key=key, sha256=upload.sha256, metadata=metadata, pipeline=pipeline
        )
        if existing is not None:
            return existing
        ref = await storage.upload(path=upload.path, file_type=upload.file_type)
        return await store.accept(
            subject=subject,
            key=key,
            upload=upload,
            metadata=metadata,
            pipeline=pipeline,
            ref=ref,
            max_object_age_seconds=timeout_seconds,
            trace_context=trace_context,
        )
