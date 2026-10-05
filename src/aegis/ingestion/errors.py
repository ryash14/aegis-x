"""Stable error categories for callers; messages never contain extracted text."""

from enum import StrEnum


class ErrorCode(StrEnum):
    SOURCE_UNREADABLE = "source_unreadable"
    OCR_UNAVAILABLE = "ocr_unavailable"
    OCR_TIMEOUT = "ocr_timeout"
    OCR_FAILED = "ocr_failed"
    INVALID_DOCX = "invalid_docx"
    INVALID_PDF = "invalid_pdf"
    ENCRYPTED_PDF = "encrypted_pdf"
    LIMIT_EXCEEDED = "limit_exceeded"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    EXTRACTION_FAILED = "extraction_failed"


class IngestionError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
