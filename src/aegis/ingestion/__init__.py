"""Local PDF ingestion and its public result/error contracts."""

from .docx import DocxBlock, DocxImage, DocxLimits, ExtractedDocx, ingest_docx
from .errors import ErrorCode, IngestionError
from .layout_models import LayoutConfig, PageLayout, TextBlock, TextLine, TextSpan
from .models import (
    DocumentMetadata,
    ExtractedDocument,
    ExtractedPage,
    IngestionLimits,
    PageStatus,
)
from .normalization import normalize_layout
from .normalization_models import NormalizationConfig, NormalizedPage, SourceSlice, TextMapping
from .ocr import extract_ocr
from .ocr_models import OCRConfig, OCREngine, OCRPage, OCRWord
from .pdf import ingest_pdf

__all__ = [
    "OCRConfig",
    "OCREngine",
    "OCRPage",
    "OCRWord",
    "extract_ocr",
    "DocxBlock",
    "DocxImage",
    "DocxLimits",
    "ExtractedDocx",
    "ingest_docx",
    "DocumentMetadata",
    "ErrorCode",
    "ExtractedDocument",
    "ExtractedPage",
    "IngestionError",
    "IngestionLimits",
    "LayoutConfig",
    "NormalizationConfig",
    "NormalizedPage",
    "PageLayout",
    "PageStatus",
    "TextBlock",
    "TextLine",
    "TextSpan",
    "SourceSlice",
    "TextMapping",
    "ingest_pdf",
    "normalize_layout",
]
