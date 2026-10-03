"""Upload request identity and the replay, content-match and replacement decisions."""

import hashlib
import json
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field

from horizon_ingestion.domain.chunking import contains_control
from horizon_ingestion.domain.documents import Lifecycle, StrictModel


def _single_line(value: str) -> str:
    if contains_control(value, allow_line_breaks=False):
        raise ValueError("control characters are not allowed")
    return value


def _free_text(value: str) -> str:
    if contains_control(value, allow_line_breaks=True):
        raise ValueError("control characters other than tab and newline are not allowed")
    return value


# PostgreSQL text cannot store NUL, and other controls make names and labels unreadable;
# rejecting them here keeps the request invalid before any original is stored.
type SingleLineText = Annotated[str, AfterValidator(_single_line)]
type FreeText = Annotated[str, AfterValidator(_free_text)]


class UploadMetadata(StrictModel):
    document_id: UUID | None = None
    corpus: SingleLineText = ""
    project_metadata: dict[SingleLineText, FreeText] = Field(default_factory=dict)


class AcceptanceConflict(StrEnum):
    """Why an upload cannot be accepted as asked; each value is the public conflict code."""

    IDEMPOTENCY_KEY = "idempotency_key_conflict"
    CONTENT_OF_ANOTHER_DOCUMENT = "content_belongs_to_another_document"
    DOCUMENT_NOT_LIVE = "document_not_live"
    REPLACEMENT_IN_PROGRESS = "replacement_in_progress"


def request_fingerprint(*, sha256: str, metadata: UploadMetadata) -> str:
    """Business input of one upload request; a replayed key must carry the same input."""
    serialized = json.dumps(
        {"sha256": sha256, "metadata": metadata.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def replay_conflict(*, stored_fingerprint: str, fingerprint: str) -> AcceptanceConflict | None:
    """None means the replayed key carries the original input and returns its job."""
    return AcceptanceConflict.IDEMPOTENCY_KEY if stored_fingerprint != fingerprint else None


def content_match_conflict(
    *, requested_document_id: UUID | None, matched_document_id: UUID
) -> AcceptanceConflict | None:
    """None means identical content may be deduplicated onto the matched document."""
    if requested_document_id is not None and matched_document_id != requested_document_id:
        return AcceptanceConflict.CONTENT_OF_ANOTHER_DOCUMENT
    return None


def replacement_conflict(
    *, lifecycle: Lifecycle, has_active_index_job: bool
) -> AcceptanceConflict | None:
    """None means a new version may replace the document's current one."""
    if lifecycle != Lifecycle.LIVE:
        return AcceptanceConflict.DOCUMENT_NOT_LIVE
    if has_active_index_job:
        return AcceptanceConflict.REPLACEMENT_IN_PROGRESS
    return None
