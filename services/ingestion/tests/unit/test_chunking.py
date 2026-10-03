"""Sliding windows stop at exact ends and retain every intersecting source interval."""

from uuid import UUID

import pytest

from horizon_ingestion.domain.chunking import (
    Extraction,
    NoExtractableTextError,
    TextUnit,
    build_manifest,
    pipeline_fingerprint,
    strip_control,
)
from horizon_ingestion.domain.documents import FileType, Pipeline

VERSION = UUID("00000000-0000-0000-0000-000000000001")


def pipeline(*, size: int = 1000, overlap: int = 200) -> Pipeline:
    return Pipeline(
        window_size=size, overlap=overlap, embedding_model_id="amazon.titan-embed-text-v2:0"
    )


@pytest.mark.parametrize(
    ("length", "intervals"),
    [
        pytest.param(10, ((0, 10),), id="short"),
        pytest.param(1000, ((0, 1000),), id="exact-window"),
        pytest.param(1800, ((0, 1000), (800, 1800)), id="exact-final-end"),
        pytest.param(2500, ((0, 1000), (800, 1800), (1600, 2500)), id="final-tail"),
    ],
)
def test_fixed_windows_keep_the_tail_once(
    length: int, intervals: tuple[tuple[int, int], ...]
) -> None:
    chunks = build_manifest(
        version_id=VERSION,
        extraction=Extraction(units=(TextUnit(text="🛰" * length, page=1),), title="Evidence"),
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=pipeline(),
    )
    assert tuple((chunk.start_offset, chunk.end_offset) for chunk in chunks) == intervals
    assert all(chunk.text for chunk in chunks)


def test_normalization_preserves_cross_page_intervals() -> None:
    extraction = Extraction(
        units=(TextUnit(text="a\r\nb", page=1), TextUnit(text="c\rd", page=2)), title="Title"
    )
    chunks = build_manifest(
        version_id=VERSION,
        extraction=extraction,
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=pipeline(size=5, overlap=1),
    )
    assert chunks[0].text == "a\nb\nc"
    assert tuple(locator.page for locator in chunks[0].locators) == (1, 2)
    assert tuple((locator.start_offset, locator.end_offset) for locator in chunks[0].locators) == (
        (0, 3),
        (4, 7),
    )
    repeated = build_manifest(
        version_id=VERSION,
        extraction=extraction,
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=pipeline(size=5, overlap=1),
    )
    assert repeated == chunks
    assert pipeline_fingerprint(pipeline(size=5, overlap=1)) != pipeline_fingerprint(
        pipeline(size=5, overlap=0)
    )


def test_empty_document_is_not_published_as_an_empty_manifest() -> None:
    with pytest.raises(NoExtractableTextError):
        build_manifest(
            version_id=VERSION,
            extraction=Extraction(units=(), title="empty"),
            filename="a.pdf",
            file_type=FileType.PDF,
            pipeline=pipeline(),
        )


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        pytest.param("Horizon\x00evidence", "Horizonevidence", id="nul"),
        pytest.param("a\x01\x08\x0b\x0c\x0e\x1fb", "ab", id="other-c0"),
        pytest.param("keep\ttab\nand newline", "keep\ttab\nand newline", id="tab-newline"),
        pytest.param("ünïcode 🛰\x7f", "ünïcode 🛰\x7f", id="beyond-c0"),
    ],
)
def test_strip_control_removes_c0_except_tab_and_newline(raw: str, clean: str) -> None:
    assert strip_control(raw) == clean


def test_control_characters_never_reach_a_chunk_or_title() -> None:
    chunks = build_manifest(
        version_id=VERSION,
        extraction=Extraction(
            units=(TextUnit(text="Hori\x00zon\r\nevidence\x07", page=1),),
            title="Ti\x00tle",
        ),
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=pipeline(),
    )
    assert [chunk.text for chunk in chunks] == ["Horizon\nevidence"]
    assert chunks[0].title == "Title"
    assert chunks[0].locators[0].end_offset == len("Horizon\nevidence")


def test_versions_indexed_under_the_old_normalization_rebuild_the_same_manifest() -> None:
    stored = pipeline().model_dump(mode="json") | {"normalization": "crlf-cr-to-lf"}
    old = Pipeline.model_validate(stored)
    assert old.normalization == "crlf-cr-to-lf"
    assert pipeline_fingerprint(old) != pipeline_fingerprint(pipeline())
    chunks = build_manifest(
        version_id=VERSION,
        extraction=Extraction(units=(TextUnit(text="a\x07b", page=1),), title="T"),
        filename="a.pdf",
        file_type=FileType.PDF,
        pipeline=old,
    )
    assert [chunk.text for chunk in chunks] == ["a\x07b"]
