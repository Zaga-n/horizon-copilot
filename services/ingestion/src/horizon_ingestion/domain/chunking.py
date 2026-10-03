"""Deterministic document-wide Unicode windows retaining intersecting source intervals."""

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid5

from horizon_ingestion.domain.documents import FileType, Locator, Pipeline

# C0 controls except tab and newline (carriage returns are normalized to newlines first).
# PostgreSQL text cannot store NUL, and none of them carries evidence.
_ALL_C0 = frozenset(range(0x20))
_CONTROL = dict.fromkeys(code for code in _ALL_C0 if chr(code) not in "\t\n")


def strip_control(text: str) -> str:
    """Remove NUL and every other C0 control character except tab and newline."""
    return text.translate(_CONTROL)


def contains_control(text: str, *, allow_line_breaks: bool) -> bool:
    """Whether `text` holds a C0 control character; tab and newline count unless allowed."""
    forbidden = _CONTROL if allow_line_breaks else _ALL_C0
    return any(ord(char) in forbidden for char in text)


class NoExtractableTextError(Exception):
    """The extraction holds no non-whitespace text, so the document has no evidence."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TextUnit:
    text: str
    page: int | None = None
    section_heading: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Extraction:
    units: tuple[TextUnit, ...]
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Chunk:
    id: UUID
    ordinal: int
    text: str
    content_hash: str
    start_offset: int
    end_offset: int
    locators: tuple[Locator, ...]
    title: str
    filename: str
    file_type: FileType


def pipeline_fingerprint(pipeline: Pipeline) -> str:
    return hashlib.sha256(pipeline.model_dump_json().encode()).hexdigest()


def build_manifest(
    *,
    version_id: UUID,
    extraction: Extraction,
    filename: str,
    file_type: FileType,
    pipeline: Pipeline,
) -> tuple[Chunk, ...]:
    if pipeline.overlap >= pipeline.window_size:
        raise ValueError("overlap must be smaller than window_size")
    sanitize = pipeline.normalization == "crlf-cr-to-lf+c0"
    parts: list[str] = []
    sources: list[Locator] = []
    offset = 0
    for unit in extraction.units:
        text = unit.text.replace("\r\n", "\n").replace("\r", "\n")
        if sanitize:
            text = strip_control(text)
        if not text:
            continue
        if parts:
            parts.append(pipeline.separator)
            offset += len(pipeline.separator)
        sources.append(
            Locator(
                page=unit.page,
                section_heading=unit.section_heading,
                start_offset=offset,
                end_offset=offset + len(text),
            )
        )
        parts.append(text)
        offset += len(text)
    full_text = "".join(parts)
    if not full_text.strip():
        raise NoExtractableTextError("no_extractable_text")
    return _windows(
        version_id=version_id,
        text=full_text,
        sources=tuple(sources),
        title=strip_control(extraction.title) if sanitize else extraction.title,
        filename=filename,
        file_type=file_type,
        pipeline=pipeline,
    )


def _windows(
    *,
    version_id: UUID,
    text: str,
    sources: tuple[Locator, ...],
    title: str,
    filename: str,
    file_type: FileType,
    pipeline: Pipeline,
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + pipeline.window_size, len(text))
        window = text[start:end]
        # Whitespace is retained in offsets; whitespace-only windows carry no evidence.
        if window.strip():
            ordinal = len(chunks)
            locators = tuple(
                source
                for source in sources
                if source.start_offset is not None
                and source.end_offset is not None
                and source.start_offset < end
                and source.end_offset > start
            )
            chunks.append(
                Chunk(
                    id=uuid5(version_id, str(ordinal)),
                    ordinal=ordinal,
                    text=window,
                    content_hash=hashlib.sha256(window.encode()).hexdigest(),
                    start_offset=start,
                    end_offset=end,
                    locators=locators,
                    title=title,
                    filename=filename,
                    file_type=file_type,
                )
            )
        if end == len(text):
            break
        start += pipeline.window_size - pipeline.overlap
    return tuple(chunks)
