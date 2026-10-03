"""Citation sources snapshotted into answers; their documents may later disappear."""

from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field

from horizon_chat.domain.values import StrictModel


class Locator(StrictModel):
    """Read side of ingestion's locator: keys this service does not know are ignored.

    Ingestion's writer model stays strict; a reader tolerating additions lets ingestion add a
    locator field without failing every turn that cites the chunk.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    page: Annotated[int, Field(gt=0)] | None = None
    section_heading: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class Source(StrictModel):
    marker: str
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    title: str
    filename: str
    file_type: str
    page: int | None = None
    section_heading: str | None = None
    locators: tuple[Locator, ...] = ()
    start_offset: int | None = None
    end_offset: int | None = None
    unavailable: bool = False
