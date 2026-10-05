"""Exercise extraction and failure handling with generated local PDFs."""

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pymupdf
import pytest

from aegis.ingestion import ErrorCode, IngestionError, IngestionLimits, PageStatus, ingest_pdf


def write_pdf(path: Path, texts: tuple[str, ...] = ("Mission report",)) -> Path:
    with pymupdf.open() as document:
        document.set_metadata({"title": "Engine test", "author": "Research team"})
        for text in texts:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        document.save(path)
    return path


def test_text_metadata_and_provenance(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf", ("Engine pressure: 42 kPa", "Temperature: 300 K"))

    result = ingest_pdf(path)

    assert result.document_id == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.source_path == str(path)
    assert result.size_bytes == path.stat().st_size
    assert result.metadata.title == "Engine test"
    assert result.metadata.author == "Research team"
    assert result.parser_version == pymupdf.VersionBind
    assert not result.repaired
    assert [page.number for page in result.pages] == [1, 2]
    assert [page.text.strip() for page in result.pages] == [
        "Engine pressure: 42 kPa",
        "Temperature: 300 K",
    ]
    assert all(page.status == PageStatus.TEXT for page in result.pages)
    assert all(page.width_points > 0 and page.height_points > 0 for page in result.pages)
    assert ingest_pdf(path) == result
    with pytest.raises(FrozenInstanceError):
        result.document_id = "changed"


def test_identity_follows_content_not_filename(tmp_path: Path) -> None:
    original = write_pdf(tmp_path / "original.pdf")
    copy = tmp_path / "renamed.bin"
    copy.write_bytes(original.read_bytes())
    changed = write_pdf(tmp_path / "changed.pdf", ("Changed report",))

    assert ingest_pdf(original).document_id == ingest_pdf(copy).document_id
    assert ingest_pdf(original).document_id != ingest_pdf(changed).document_id


def test_mixed_document_retains_missing_text_pages(tmp_path: Path) -> None:
    path = tmp_path / "mixed.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Text page")
        image_page = document.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
        pixmap.clear_with(255)
        image_page.insert_image(pymupdf.Rect(20, 20, 120, 120), pixmap=pixmap)
        document.new_page()
        document.save(path)

    result = ingest_pdf(path)

    assert [page.number for page in result.pages] == [1, 2, 3]
    assert [page.status for page in result.pages] == [
        PageStatus.TEXT,
        PageStatus.IMAGE_ONLY,
        PageStatus.NO_TEXT,
    ]
    assert result.pages[1].text == result.pages[2].text == ""


@pytest.mark.parametrize("payload", [b"", b"Not a PDF", b"%PDF-1.7\nbroken"])
def test_invalid_pdf(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "invalid.pdf"
    path.write_bytes(payload)

    with pytest.raises(IngestionError) as error:
        ingest_pdf(path)

    assert error.value.code == ErrorCode.INVALID_PDF


@pytest.mark.parametrize("directory", [False, True])
def test_unreadable_source(tmp_path: Path, directory: bool) -> None:
    source = tmp_path if directory else tmp_path / "missing.pdf"

    with pytest.raises(IngestionError) as error:
        ingest_pdf(source)

    assert error.value.code == ErrorCode.SOURCE_UNREADABLE


@pytest.mark.parametrize("user_password", ["secret", ""])
def test_encrypted_pdf(tmp_path: Path, user_password: str) -> None:
    path = tmp_path / "encrypted.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Confidential")
        document.save(
            path,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw=user_password,
        )

    with pytest.raises(IngestionError) as error:
        ingest_pdf(path)

    assert error.value.code == ErrorCode.ENCRYPTED_PDF


@pytest.mark.parametrize("image_only", [False, True])
def test_pdf_without_extractable_text(tmp_path: Path, image_only: bool) -> None:
    path = tmp_path / "no-text.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        if image_only:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
            pixmap.clear_with(255)
            page.insert_image(pymupdf.Rect(20, 20, 120, 120), pixmap=pixmap)
        document.save(path)

    with pytest.raises(IngestionError) as error:
        ingest_pdf(path)

    assert error.value.code == ErrorCode.NO_EXTRACTABLE_TEXT


def test_configurable_limits(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "large.pdf", ("Page one", "Page two"))
    for limits in (IngestionLimits(max_file_bytes=10), IngestionLimits(max_pages=1)):
        with pytest.raises(IngestionError) as error:
            ingest_pdf(path, limits=limits)
        assert error.value.code == ErrorCode.LIMIT_EXCEEDED

    assert (
        len(ingest_pdf(path, limits=IngestionLimits(max_file_bytes=path.stat().st_size)).pages) == 2
    )


@pytest.mark.parametrize("kwargs", [{"max_file_bytes": 0}, {"max_pages": -1}])
def test_invalid_limits(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        IngestionLimits(**kwargs)


def test_reading_order(tmp_path: Path) -> None:
    path = tmp_path / "order.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 144), "Lower line")
        page.insert_text((72, 72), "Upper line")
        document.save(path)

    text = ingest_pdf(path).pages[0].text
    assert text.index("Upper line") < text.index("Lower line")


def test_logging_excludes_text_and_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = write_pdf(tmp_path / "private-name.pdf", ("PRIVATE TEXT",))
    with caplog.at_level("INFO", logger="aegis.ingestion.pdf"):
        result = ingest_pdf(path)

    assert len(caplog.records) == 1
    assert caplog.records[0].document_id == result.document_id
    assert caplog.records[0].page_count == 1
    assert "PRIVATE TEXT" not in caplog.text
    assert str(path) not in caplog.text
