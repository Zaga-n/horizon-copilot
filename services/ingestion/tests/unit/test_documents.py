"""Real parsers preserve PDF pages and Word sections and reject malformed uploads."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pytest

from horizon_ingestion.adapters.documents import DocumentExtractor, DocumentPreparer
from horizon_ingestion.domain.documents import ExtractionPolicy, FileType
from horizon_ingestion.ports.uploads import UploadValidationError
from horizon_ingestion_testing.files import pdf_bytes, word_bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class Stream:
    source: BytesIO

    async def read(self, size: int = -1) -> bytes:
        return self.source.read(size)


def policy() -> ExtractionPolicy:
    return ExtractionPolicy(
        memory_bytes=536870912, max_chars=1000000, max_units=100, timeout_seconds=5
    )


async def test_pdf_page_provenance(tmp_path: Path) -> None:
    path = tmp_path / "pages.pdf"
    path.write_bytes(pdf_bytes(pages=("First page", "Second page")))
    extracted = await DocumentExtractor(policy=policy()).extract(
        path=path, file_type=FileType.PDF, filename="pages.pdf"
    )
    assert [(unit.page, unit.text.strip()) for unit in extracted.units] == [
        (1, "First page"),
        (2, "Second page"),
    ]
    assert extracted.title == "pages.pdf"


async def test_word_section_provenance(tmp_path: Path) -> None:
    path = tmp_path / "sections.docx"
    path.write_bytes(word_bytes())
    extracted = await DocumentExtractor(policy=policy()).extract(
        path=path, file_type=FileType.DOCX, filename="sections.docx"
    )
    assert extracted.title == "Scope"
    assert extracted.units[-1].section_heading == "Scope"
    assert extracted.units[-1].page is None


async def test_missing_word_heading_stays_absent(tmp_path: Path) -> None:
    path = tmp_path / "plain.docx"
    path.write_bytes(word_bytes(heading=None))
    extracted = await DocumentExtractor(policy=policy()).extract(
        path=path, file_type=FileType.DOCX, filename="plain.docx"
    )
    assert extracted.title == "plain.docx"
    assert extracted.units[0].section_heading is None


@pytest.mark.parametrize(
    ("filename", "content_type", "content", "reason"),
    [
        pytest.param(
            "a.doc", "application/msword", b"old word", "unsupported_file", id="legacy-doc"
        ),
        pytest.param("a.pdf", "application/pdf", b"not pdf", "malformed_file", id="malformed-pdf"),
        pytest.param(
            "a.docx", "application/pdf", b"pdf", "file_type_mismatch", id="declared-mismatch"
        ),
        pytest.param("a.pdf", "application/pdf", b"x" * 101, "file_too_large", id="oversize"),
    ],
)
async def test_invalid_upload_never_yields_an_original(
    filename: str, content_type: str, content: bytes, reason: str
) -> None:
    preparer = DocumentPreparer(max_bytes=100, extraction_policy=policy())
    with pytest.raises(UploadValidationError, match=reason):
        async with preparer.prepare(
            stream=Stream(source=BytesIO(content)), filename=filename, content_type=content_type
        ):
            pytest.fail("invalid original was accepted")


async def test_validated_temporary_original_is_removed_after_use() -> None:
    preparer = DocumentPreparer(max_bytes=100000, extraction_policy=policy())
    async with preparer.prepare(
        stream=Stream(source=BytesIO(word_bytes())),
        filename="scope.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) as prepared:
        path = prepared.path
        assert path.exists()
        assert len(prepared.sha256) == 64
    assert not path.exists()
