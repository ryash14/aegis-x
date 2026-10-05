"""Immutable extraction records and configurable ingestion limits."""

from dataclasses import dataclass
from enum import StrEnum

from .layout_models import PageLayout
from .normalization_models import NormalizedPage


class PageStatus(StrEnum):
    TEXT = "text"
    IMAGE_ONLY = "image_only"
    NO_TEXT = "no_text"


@dataclass(frozen=True, slots=True)
class IngestionLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_pages: int = 2000

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_pages <= 0:
            raise ValueError("Ingestion limits must be positive")


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    title: str
    author: str
    subject: str
    creator: str
    producer: str
    creation_date: str
    modification_date: str


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    number: int
    text: str
    width_points: float
    height_points: float
    status: PageStatus
    layout: PageLayout | None = None
    normalized: NormalizedPage | None = None


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    document_id: str
    source_path: str
    size_bytes: int
    metadata: DocumentMetadata
    pages: tuple[ExtractedPage, ...]
    parser_version: str
    repaired: bool
