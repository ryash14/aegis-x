"""OCR correctness, coordinate provenance, bounded work, and engine failures."""

import json
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest

from aegis.ingestion import ErrorCode, IngestionError, OCRConfig, PageStatus, ingest_pdf
from aegis.ingestion.batch import BatchConfig, _pipeline_id, run_batch
from aegis.ingestion.ocr import _parse_tsv, _run, engine_identity, extract_ocr
from aegis.ingestion.ocr_models import OCREngine

ENGINE = OCREngine("test", "hash", (("eng", "model"),))
TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext\n"
)
requires_engine = pytest.mark.skipif(
    not shutil.which("tesseract"), reason="Local Tesseract required"
)


def scanned_pdf(path: Path, rotation: int = 0, mixed: bool = False) -> Path:
    with pymupdf.open() as original:
        page = original.new_page(width=500, height=450)
        for index in range(9):
            page.insert_text(
                (40, 50 + index * 38),
                "Mission status nominal. Engine pressure stable.",
                fontsize=17,
            )
        image = page.get_pixmap(dpi=200, alpha=False).tobytes("png")
    with pymupdf.open() as document:
        if mixed:
            native = document.new_page()
            native.insert_text((40, 50), "Native text unchanged")
            native.insert_image(pymupdf.Rect(100, 100, 300, 280), stream=image)
        page = document.new_page(width=500, height=450)
        page.insert_image(page.rect, stream=image)
        page.set_rotation(rotation)
        document.save(path)
    return path


@requires_engine
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_real_recognition_and_page_coordinates(tmp_path, rotation):
    path = scanned_pdf(tmp_path / "scan.pdf", rotation)
    result = ingest_pdf(path, include_ocr=True)
    page = result.pages[0]
    assert page.status == PageStatus.IMAGE_ONLY and page.text == ""
    assert page.ocr.text.count("Mission status nominal.") == 9
    assert (rotation + page.ocr.rotation_correction) % 360 == 0
    assert page.ocr.engine.model_sha256
    for word in page.ocr.words:
        assert page.ocr.text[word.output_start : word.output_end] == word.text
        x0, y0, x1, y1 = word.bbox
        assert 0 <= x0 < x1 <= 500 and 0 <= y0 < y1 <= 450
    first = page.ocr.words[0]
    assert first.text == "Mission"
    assert abs(first.bbox[0] - 40) < 5 and abs(first.bbox[1] - 38) < 8
    with pymupdf.open(path) as source:
        assert source[0].rotation == rotation


@requires_engine
def test_mixed_pdf_keeps_native_text_and_ocr_separate(tmp_path):
    path = scanned_pdf(tmp_path / "mixed.pdf", mixed=True)
    result = ingest_pdf(path, include_ocr=True, include_normalized=True)
    assert result.pages[0].text == ingest_pdf(path).pages[0].text
    assert result.pages[0].ocr is None
    assert result.pages[0].warnings
    assert result.pages[1].ocr.words
    assert result.pages[1].normalized.text == ""


def test_pixel_limit_checked_before_engine_or_render(monkeypatch):
    monkeypatch.setattr(
        "aegis.ingestion.ocr.engine_identity", lambda config: pytest.fail("Engine called")
    )
    with pymupdf.open() as document:
        page = document.new_page()
        with pytest.raises(IngestionError) as caught:
            extract_ocr(page, OCRConfig(max_pixels=1))
    assert caught.value.code == ErrorCode.LIMIT_EXCEEDED


def test_missing_engine_is_explicit(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(IngestionError) as caught:
        engine_identity(OCRConfig())
    assert caught.value.code == ErrorCode.OCR_UNAVAILABLE


def test_timeout_is_classified(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(IngestionError) as caught:
        _run(["tesseract"], 1)
    assert caught.value.code == ErrorCode.OCR_TIMEOUT


def test_failed_recognition_restores_rotation(monkeypatch):
    def run(arguments, timeout):
        if "0" in arguments:
            return subprocess.CompletedProcess(
                arguments, 0, "Rotate: 90\nOrientation confidence: 20\n", ""
            )
        return subprocess.CompletedProcess(arguments, 1, "", "failure")

    monkeypatch.setattr("aegis.ingestion.ocr._run", run)
    with pymupdf.open() as document:
        page = document.new_page()
        page.set_rotation(180)
        with pytest.raises(IngestionError) as caught:
            extract_ocr(page, engine=ENGINE)
        assert page.rotation == 180
    assert caught.value.code == ErrorCode.OCR_FAILED


def test_tsv_preserves_offsets_and_hierarchy():
    rows = (
        "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t95\tOne\n"
        "5\t1\t1\t1\t1\t2\t50\t20\t30\t10\t65\ttwo\n"
        "5\t1\t1\t1\t2\t1\t10\t40\t30\t10\t90\tThree\n"
    )
    text, words = _parse_tsv(TSV + rows, 200, 100, pymupdf.Rect(0, 0, 100, 50), pymupdf.Identity)
    assert text == "One two\nThree"
    assert words[0].bbox == (5, 10, 20, 15)
    assert all(text[word.output_start : word.output_end] == word.text for word in words)


@pytest.mark.parametrize("confidence,left", [("nan", 10), ("-1", 10), ("95", -10), ("95", 190)])
def test_invalid_engine_data(confidence, left):
    row = f"5\t1\t1\t1\t1\t1\t{left}\t20\t30\t10\t{confidence}\tBad\n"
    with pytest.raises(IngestionError) as caught:
        _parse_tsv(TSV + row, 200, 100, pymupdf.Rect(0, 0, 100, 50), pymupdf.Identity)
    assert caught.value.code == ErrorCode.OCR_FAILED


@pytest.mark.parametrize(
    "settings",
    [
        {"dpi": 0},
        {"max_pixels": 0},
        {"timeout_seconds": float("nan")},
        {"language": "../eng"},
        {"low_confidence_threshold": 101},
    ],
)
def test_invalid_configuration(settings):
    with pytest.raises(ValueError):
        OCRConfig(**settings)


@requires_engine
def test_ocr_batch_resumes_and_fingerprints_settings(tmp_path):
    path = scanned_pdf(tmp_path / "scan.pdf")
    config = BatchConfig(mode="ocr")
    output = tmp_path / "output"
    first = run_batch(path, output, config)
    assert first["succeeded"] == 1
    record = json.loads(Path(first["jobs"][0]["output_path"]).read_text())
    assert record["document"]["pages"][0]["ocr"]["words"]
    assert run_batch(path, output, config)["cached"] == 1
    assert _pipeline_id(config) != _pipeline_id(BatchConfig(mode="ocr", ocr=OCRConfig(dpi=250)))


def test_missing_tsv_columns_rejected():
    with pytest.raises(IngestionError):
        _parse_tsv("unexpected\n", 200, 100, pymupdf.Rect(0, 0, 100, 50), pymupdf.Identity)


@requires_engine
def test_missing_language_is_explicit():
    with pytest.raises(IngestionError) as caught:
        engine_identity(OCRConfig(language="aegis_nonexistent"))
    assert caught.value.code == ErrorCode.OCR_UNAVAILABLE


def test_blank_ocr_preserves_warning(monkeypatch):
    def run(arguments, timeout):
        if "0" in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "too few characters")
        return subprocess.CompletedProcess(arguments, 0, TSV, "")

    monkeypatch.setattr("aegis.ingestion.ocr._run", run)
    with pymupdf.open() as document:
        result = extract_ocr(document.new_page(), engine=ENGINE)
    assert not result.text and not result.words
    assert "No text recognized" in result.warnings
    assert result.orientation_confidence is None


@requires_engine
def test_model_identity_changes_invalidate_batch(monkeypatch):
    first = _pipeline_id(BatchConfig(mode="ocr"))
    monkeypatch.setattr("aegis.ingestion.batch.engine_identity", lambda config: ENGINE)
    assert _pipeline_id(BatchConfig(mode="ocr")) != first


def test_native_only_pdf_does_not_require_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aegis.ingestion.pdf.engine_identity", lambda config: pytest.fail("OCR called")
    )
    path = tmp_path / "native.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 50), "Native content")
        document.save(path)
    result = ingest_pdf(path, include_ocr=True)
    assert result.pages[0].text == "Native content"
    assert result.pages[0].ocr is None
