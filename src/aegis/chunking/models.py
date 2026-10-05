"""Immutable chunks with explicit output-to-source mappings."""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    max_chars: int = 1800
    overlap_chars: int = 160
    infer_pdf_headings: bool = True
    heading_font_ratio: float = 1.3
    max_heading_chars: int = 160

    def __post_init__(self) -> None:
        if self.max_chars < 1 or not 0 <= self.overlap_chars < self.max_chars:
            raise ValueError("Chunk size must be positive and overlap smaller than chunk size")
        if not math.isfinite(self.heading_font_ratio) or self.heading_font_ratio <= 1:
            raise ValueError("Heading font ratio must be finite and exceed one")
        if self.max_heading_chars < 1:
            raise ValueError("Heading text limit must be positive")


@dataclass(frozen=True, slots=True)
class ChunkSource:
    domain: str
    start: int
    end: int
    page: int | None = None
    block_id: int | None = None
    line_index: int | None = None
    xml_path: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ChunkMapping:
    output_start: int
    output_end: int
    sources: tuple[ChunkSource, ...]
    kind: str


@dataclass(frozen=True, slots=True)
class ChunkHeading:
    text: str
    level: int
    method: str
    source: ChunkSource


@dataclass(frozen=True, slots=True)
class TextUnit:
    unit_id: str
    text: str
    kind: str
    group: str
    headings: tuple[ChunkHeading, ...]
    mappings: tuple[ChunkMapping, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChunkFragment:
    unit_id: str
    unit_start: int
    unit_end: int
    output_start: int
    output_end: int
    overlap_prefix_chars: int = 0


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    document_id: str
    source_path: str
    index: int
    text: str
    kind: str
    headings: tuple[ChunkHeading, ...]
    fragments: tuple[ChunkFragment, ...]
    mappings: tuple[ChunkMapping, ...]
    warnings: tuple[str, ...]
    algorithm: str = "structure-v1"
