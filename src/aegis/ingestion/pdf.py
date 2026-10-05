"""Read and hash one bounded file snapshot, then extract locally with PyMuPDF."""

import hashlib
import logging
from pathlib import Path

import pymupdf

from .errors import ErrorCode, IngestionError
from .layout import extract_layout
from .layout_models import LayoutConfig
from .models import (
    DocumentMetadata,
    ExtractedDocument,
    ExtractedPage,
    IngestionLimits,
    PageStatus,
)
from .normalization import normalize_layout
from .normalization_models import NormalizationConfig

logger = logging.getLogger(__name__)


def ingest_pdf(
    source: str | Path,
    *,
    limits: IngestionLimits | None = None,
    include_layout: bool = False,
    layout_config: LayoutConfig | None = None,
    include_normalized: bool = False,
    normalization_config: NormalizationConfig | None = None,
) -> ExtractedDocument:
    """Extract PDF text without OCR, network calls, or persistent writes.

    Pages use one-based physical PDF numbering. Image-only detection is a
    heuristic, not a diagnosis of scanning. Mixed documents retain every page.
    """
    limits = limits if limits is not None else IngestionLimits()
    selected_layout_config = layout_config if layout_config is not None else LayoutConfig()
    path = Path(source).expanduser().absolute()
    try:
        if not path.is_file():
            raise IngestionError(ErrorCode.SOURCE_UNREADABLE, "Source must be a readable file")
        with path.open("rb") as stream:
            if stream.seek(0, 2) > limits.max_file_bytes:
                raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "PDF exceeds the file size limit")
            stream.seek(0)
            snapshot = stream.read(limits.max_file_bytes + 1)
    except OSError as exc:
        raise IngestionError(ErrorCode.SOURCE_UNREADABLE, "Could not read source file") from exc
    if len(snapshot) > limits.max_file_bytes:
        raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "PDF exceeds the file size limit")
    if not snapshot.startswith(b"%PDF-"):
        raise IngestionError(ErrorCode.INVALID_PDF, "Source lacks a PDF header")

    # Hash the exact bytes parsed, avoiding identity changes between separate reads.
    document_id = hashlib.sha256(snapshot).hexdigest()
    try:
        document = pymupdf.open(stream=snapshot, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise IngestionError(ErrorCode.INVALID_PDF, "Could not open PDF") from exc

    with document:
        if document.needs_pass or document.is_encrypted:
            raise IngestionError(ErrorCode.ENCRYPTED_PDF, "Password-protected PDFs are unsupported")
        if (document.metadata or {}).get("encryption"):
            raise IngestionError(ErrorCode.ENCRYPTED_PDF, "Encrypted PDFs are unsupported")
        if document.page_count > limits.max_pages:
            raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "PDF exceeds the page limit")
        try:
            metadata = document.metadata or {}
            pages = []
            for number, page in enumerate(document, start=1):
                text = page.get_text("text", sort=True)
                status = PageStatus.TEXT
                if not text.strip():
                    status = PageStatus.IMAGE_ONLY if page.get_image_info() else PageStatus.NO_TEXT
                layout = (
                    extract_layout(page, selected_layout_config)
                    if include_layout or include_normalized
                    else None
                )
                normalized = (
                    normalize_layout(layout, normalization_config)
                    if include_normalized and layout
                    else None
                )
                pages.append(
                    ExtractedPage(
                        number, text, page.rect.width, page.rect.height, status, layout, normalized
                    )
                )
            if not any(page.status == PageStatus.TEXT for page in pages):
                raise IngestionError(
                    ErrorCode.NO_EXTRACTABLE_TEXT,
                    "PDF has no extractable text; image-only pages may require OCR",
                )
            result = ExtractedDocument(
                document_id=document_id,
                source_path=str(path),
                size_bytes=len(snapshot),
                metadata=DocumentMetadata(
                    title=metadata.get("title") or "",
                    author=metadata.get("author") or "",
                    subject=metadata.get("subject") or "",
                    creator=metadata.get("creator") or "",
                    producer=metadata.get("producer") or "",
                    creation_date=metadata.get("creationDate") or "",
                    modification_date=metadata.get("modDate") or "",
                ),
                pages=tuple(pages),
                parser_version=pymupdf.VersionBind,
                repaired=document.is_repaired,
            )
        except (RuntimeError, ValueError) as exc:
            raise IngestionError(ErrorCode.EXTRACTION_FAILED, "PDF extraction failed") from exc

    logger.info(
        "PDF ingestion completed",
        extra={"document_id": document_id, "page_count": len(result.pages)},
    )
    return result
