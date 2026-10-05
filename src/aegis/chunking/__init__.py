"""Structure-aware chunking for immutable extracted PDF and DOCX documents."""

from .chunker import chunk_document, iter_chunks
from .models import (
    Chunk,
    ChunkConfig,
    ChunkFragment,
    ChunkHeading,
    ChunkMapping,
    ChunkSource,
    TextUnit,
)
from .units import iter_units

__all__ = [
    "Chunk",
    "ChunkConfig",
    "ChunkFragment",
    "ChunkHeading",
    "ChunkMapping",
    "ChunkSource",
    "TextUnit",
    "chunk_document",
    "iter_chunks",
    "iter_units",
]
