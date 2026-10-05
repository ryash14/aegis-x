"""Offline Tesseract subprocesses with bounded rasters and page-space word boxes."""

import csv
import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pymupdf

from .errors import ErrorCode, IngestionError
from .ocr_models import OCRConfig, OCREngine, OCRPage, OCRWord


def _run(arguments: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except FileNotFoundError as exc:
        raise IngestionError(ErrorCode.OCR_UNAVAILABLE, "Tesseract is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise IngestionError(ErrorCode.OCR_TIMEOUT, "OCR page timed out") from exc
    except OSError as exc:
        raise IngestionError(ErrorCode.OCR_FAILED, "Cannot start local OCR engine") from exc


def engine_identity(config: OCRConfig) -> OCREngine:
    """Hash actual executable and recognition models for reproducible cache identity."""
    executable = shutil.which("tesseract")
    if not executable:
        raise IngestionError(ErrorCode.OCR_UNAVAILABLE, "Install Tesseract and language data")
    version = _run([executable, "--version"], 10)
    languages = _run([executable, "--list-langs"], 10)
    match = re.search(r'"([^"]+)"', languages.stdout)
    if version.returncode or not version.stdout.strip() or languages.returncode or not match:
        raise IngestionError(ErrorCode.OCR_UNAVAILABLE, "Cannot identify Tesseract language data")
    required = set(config.language.split("+"))
    if config.detect_orientation:
        required.add("osd")
    models = []
    try:
        for language in sorted(required):
            with (Path(match.group(1)) / f"{language}.traineddata").open("rb") as stream:
                models.append((language, hashlib.file_digest(stream, "sha256").hexdigest()))
        with Path(executable).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise IngestionError(
            ErrorCode.OCR_UNAVAILABLE, "Required OCR language data unavailable"
        ) from exc
    return OCREngine(version.stdout.splitlines()[0], digest, tuple(models))


def _parse_tsv(
    value: str,
    width: int,
    height: int,
    rect: pymupdf.Rect,
    derotation: pymupdf.Matrix,
) -> tuple[str, tuple[OCRWord, ...]]:
    output = ""
    words = []
    previous = None
    try:
        reader = csv.DictReader(io.StringIO(value), delimiter="\t", quoting=csv.QUOTE_NONE)
        required = {
            "level",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Missing OCR columns")
        for row in reader:
            if row["level"] != "5" or not row["text"].strip():
                continue
            confidence = float(row["conf"])
            if not math.isfinite(confidence) or not 0 <= confidence <= 100:
                raise ValueError("Invalid confidence")
            key = tuple(int(row[k]) for k in ("block_num", "par_num", "line_num"))
            left, top, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            if min(left, top, w, h) < 0 or left + w > width or top + h > height:
                raise ValueError("Invalid OCR geometry")
            if previous is not None:
                output += " " if key == previous else "\n" if key[:2] == previous[:2] else "\n\n"
            start = len(output)
            output += row["text"]
            box = pymupdf.Rect(
                left * rect.width / width,
                top * rect.height / height,
                (left + w) * rect.width / width,
                (top + h) * rect.height / height,
            )
            box = box * derotation
            words.append(
                OCRWord(
                    row["text"],
                    tuple(box),
                    confidence,
                    *key,
                    int(row["word_num"]),
                    start,
                    len(output),
                )
            )
            previous = key
    except (KeyError, ValueError, TypeError, csv.Error) as exc:
        raise IngestionError(ErrorCode.OCR_FAILED, "Invalid OCR engine output") from exc
    return output, tuple(words)


def extract_ocr(
    page: pymupdf.Page,
    config: OCRConfig | None = None,
    *,
    engine: OCREngine | None = None,
) -> OCRPage:
    """OCR a single page. Bboxes use unrotated PDF points; source PDF is never written."""
    config = config or OCRConfig()
    width = math.ceil(page.rect.width * config.dpi / 72)
    height = math.ceil(page.rect.height * config.dpi / 72)
    if width * height > config.max_pixels:
        raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "OCR raster exceeds pixel limit")
    engine = engine or engine_identity(config)
    started = time.monotonic()
    warnings = ["OCR text requires review; confidence is not a calibrated accuracy score"]
    rotation = page.rotation
    correction = 0
    orientation_confidence = None

    def remaining() -> float:
        available = config.timeout_seconds - (time.monotonic() - started)
        if available <= 0:
            raise IngestionError(ErrorCode.OCR_TIMEOUT, "OCR page timed out")
        return available

    try:
        with tempfile.TemporaryDirectory(
            prefix="aegis-ocr-", dir=os.environ.get("AEGIS_OCR_TEMP_ROOT")
        ) as directory:
            raster = Path(directory) / "page.png"

            def render() -> pymupdf.Pixmap:
                remaining()
                image = page.get_pixmap(
                    dpi=config.dpi, colorspace=pymupdf.csRGB, alpha=False, annots=False
                )
                if image.width * image.height > config.max_pixels:
                    raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "OCR raster exceeds pixel limit")
                image.save(raster)
                return image

            image = render()
            if config.detect_orientation:
                detection = _run(["tesseract", str(raster), "stdout", "--psm", "0"], remaining())
                rotate = re.search(r"^Rotate: (0|90|180|270)$", detection.stdout, re.MULTILINE)
                confidence = re.search(
                    r"^Orientation confidence: ([0-9.]+)$", detection.stdout, re.MULTILINE
                )
                if detection.returncode == 0 and rotate and confidence:
                    correction = int(rotate.group(1))
                    orientation_confidence = float(confidence.group(1))
                    if orientation_confidence < 15:
                        warnings.append(
                            "Low-confidence orientation estimate; inspect page alignment"
                        )
                    if correction:
                        page.set_rotation((rotation + correction) % 360)
                        image = render()
                else:
                    warnings.append(
                        "Orientation detection unavailable for this page; rotation unchanged"
                    )
            result = _run(
                [
                    "tesseract",
                    str(raster),
                    "stdout",
                    "-l",
                    config.language,
                    "--dpi",
                    str(config.dpi),
                    "--psm",
                    "3",
                    "tsv",
                ],
                remaining(),
            )
            if result.returncode:
                raise IngestionError(ErrorCode.OCR_FAILED, "Tesseract recognition failed")
            text, words = _parse_tsv(
                result.stdout, image.width, image.height, page.rect, page.derotation_matrix
            )
            if not words:
                warnings.append("No text recognized")
            low = sum(word.confidence < config.low_confidence_threshold for word in words)
            if low:
                warnings.append(f"{low} of {len(words)} words below confidence threshold")
            return OCRPage(
                text,
                words,
                engine,
                config,
                correction,
                orientation_confidence,
                image.width,
                image.height,
                tuple(warnings),
                round(time.monotonic() - started, 3),
            )
    finally:
        page.set_rotation(rotation)
