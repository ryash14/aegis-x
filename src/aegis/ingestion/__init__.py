"""Local PDF ingestion and its public result/error contracts."""

from .errors import ErrorCode, IngestionError
from .layout_models import LayoutConfig, PageLayout, TextBlock, TextLine, TextSpan
from .models import (
    DocumentMetadata,
    ExtractedDocument,
    ExtractedPage,
    IngestionLimits,
    PageStatus,
)
from .pdf import ingest_pdf

__all__ = [
    "DocumentMetadata",
    "ErrorCode",
    "ExtractedDocument",
    "ExtractedPage",
    "IngestionError",
    "IngestionLimits",
    "LayoutConfig",
    "PageLayout",
    "PageStatus",
    "TextBlock",
    "TextLine",
    "TextSpan",
    "ingest_pdf",
]
