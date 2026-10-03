"""Temporary upload spooling and isolated bounded PDF/Word parser execution."""

import asyncio
import hashlib
import multiprocessing
import resource
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.oxml.exceptions import InvalidXmlError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from horizon_ingestion.domain.chunking import Extraction, TextUnit, contains_control
from horizon_ingestion.domain.documents import ExtractionPolicy, FileType
from horizon_ingestion.ports.uploads import (
    FileTooLargeError,
    PreparedUpload,
    UploadStream,
    UploadValidationError,
)

MIME_TYPES = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
READ_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentPreparer:
    """Own temporary originals only for the acceptance operation."""

    max_bytes: int
    extraction_policy: ExtractionPolicy

    @asynccontextmanager
    async def prepare(
        self, *, stream: UploadStream, filename: str, content_type: str
    ) -> AsyncIterator[PreparedUpload]:
        name = Path(filename).name
        if contains_control(name, allow_line_breaks=False):
            raise UploadValidationError("invalid_filename")
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix not in (FileType.PDF, FileType.DOCX) or not name or len(name) > 255:
            raise UploadValidationError("unsupported_file")
        file_type = FileType(suffix)
        if content_type != MIME_TYPES[file_type]:
            raise UploadValidationError("file_type_mismatch")
        with tempfile.TemporaryDirectory(prefix="horizon-upload-") as directory:
            path = Path(directory) / f"original.{file_type}"
            digest = await _spool(stream=stream, path=path, max_bytes=self.max_bytes)
            await asyncio.to_thread(
                _isolated_parse, path, file_type, name, self.extraction_policy, extract=False
            )
            yield PreparedUpload(path=path, sha256=digest, filename=name, file_type=file_type)


async def _spool(*, stream: UploadStream, path: Path, max_bytes: int) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("wb") as output:
        while block := await stream.read(READ_SIZE):
            size += len(block)
            if size > max_bytes:
                raise FileTooLargeError()
            digest.update(block)
            await asyncio.to_thread(output.write, block)
    if size == 0:
        raise UploadValidationError("empty_file")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentExtractor:
    """Run extraction outside the worker process with finite wall-time and output bounds."""

    policy: ExtractionPolicy

    async def extract(self, *, path: Path, file_type: FileType, filename: str) -> Extraction:
        return await asyncio.to_thread(
            _isolated_parse, path, file_type, filename, self.policy, extract=True
        )


def _isolated_parse(
    path: Path, file_type: FileType, filename: str, policy: ExtractionPolicy, *, extract: bool
) -> Extraction:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_parser_child, args=(child, path, file_type, filename, policy, extract)
    )
    process.start()
    child.close()
    try:
        if not parent.poll(policy.timeout_seconds):
            raise UploadValidationError("parser_timeout")
        result = parent.recv()
        if not isinstance(result, Extraction):
            raise UploadValidationError("malformed_file")
        return result
    except EOFError as exc:
        raise UploadValidationError("parser_failed") from exc
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def _parser_child(
    connection: Connection,
    path: Path,
    file_type: FileType,
    filename: str,
    policy: ExtractionPolicy,
    extract: bool,
) -> None:
    try:
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (policy.memory_bytes, policy.memory_bytes))
        result = (
            _parse_pdf(path, filename, policy, extract)
            if file_type == FileType.PDF
            else _parse_docx(path, filename, policy, extract)
        )
    except (
        PdfReadError,
        BadZipFile,
        ValueError,
        KeyError,
        InvalidXmlError,
        XMLSyntaxError,
        OSError,
        MemoryError,
    ):
        connection.send(None)
    else:
        connection.send(result)
    finally:
        connection.close()


def _parse_pdf(path: Path, filename: str, policy: ExtractionPolicy, extract: bool) -> Extraction:
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("not_pdf")
        stream.seek(0)
        reader = PdfReader(stream, strict=True)
        if reader.is_encrypted or not 0 < len(reader.pages) <= policy.max_units:
            raise ValueError("unsupported_pdf")
        title = reader.metadata.title if reader.metadata else None
        units: list[TextUnit] = []
        size = 0
        for number, page in enumerate(reader.pages, 1):
            if extract:
                text = page.extract_text() or ""
                size += len(text)
                if size > policy.max_chars:
                    raise ValueError("extracted_text_too_large")
                units.append(TextUnit(text=text, page=number))
    return Extraction(
        units=tuple(units),
        title=title.strip() if isinstance(title, str) and title.strip() else filename,
    )


def _parse_docx(path: Path, filename: str, policy: ExtractionPolicy, extract: bool) -> Extraction:
    _validate_docx_archive(path=path, max_chars=policy.max_chars)
    document = Document(str(path))
    if not extract:
        return Extraction(units=(), title=filename)
    units: list[TextUnit] = []
    heading: str | None = None
    title = document.core_properties.title.strip() or filename
    size = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text
            style = block.style.name if block.style else ""
            if style == "Title" and text.strip() and title == filename:
                title = text.strip()
            if style.startswith("Heading") and text.strip():
                heading = text.strip()
                if title == filename:
                    title = heading
        elif isinstance(block, Table):
            text = "\n".join("\t".join(cell.text for cell in row.cells) for row in block.rows)
        size += len(text)
        if size > policy.max_chars or len(units) >= policy.max_units:
            raise ValueError("extraction_limit")
        units.append(TextUnit(text=text, section_heading=heading))
    return Extraction(units=tuple(units), title=title)


def _validate_docx_archive(*, path: Path, max_chars: int) -> None:
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > max_chars * 4:
            raise ValueError("expanded_file_too_large")
        if not {"[Content_Types].xml", "word/document.xml"} <= set(archive.namelist()):
            raise ValueError("not_docx")
