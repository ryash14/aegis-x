"""OCR output stays distinct from native PDF text and normalization."""

import math
import re
from dataclasses import dataclass

from .layout_models import BoundingBox


@dataclass(frozen=True, slots=True)
class OCRConfig:
    language: str = "eng"
    dpi: int = 200
    max_pixels: int = 20_000_000
    timeout_seconds: float = 60
    detect_orientation: bool = True
    low_confidence_threshold: float = 70

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", self.language):
            raise ValueError("OCR language must be installed language codes joined with +")
        if not 72 <= self.dpi <= 600 or self.max_pixels <= 0:
            raise ValueError("OCR requires DPI 72–600 and a positive pixel limit")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("OCR timeout must be finite and positive")
        if not 0 <= self.low_confidence_threshold <= 100:
            raise ValueError("OCR confidence threshold must be 0–100")


@dataclass(frozen=True, slots=True)
class OCREngine:
    version: str
    executable_sha256: str
    model_sha256: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class OCRWord:
    text: str
    bbox: BoundingBox
    confidence: float
    block: int
    paragraph: int
    line: int
    word: int
    output_start: int
    output_end: int


@dataclass(frozen=True, slots=True)
class OCRPage:
    text: str
    words: tuple[OCRWord, ...]
    engine: OCREngine
    config: OCRConfig
    rotation_correction: int
    orientation_confidence: float | None
    raster_width: int
    raster_height: int
    warnings: tuple[str, ...]
    elapsed_seconds: float
    method: str = "tesseract-tsv-v1"
