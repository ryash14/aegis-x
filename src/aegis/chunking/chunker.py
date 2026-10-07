"""Deterministic bounded chunks; source units stay immutable."""

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import asdict, replace

from aegis.ingestion.docx import ExtractedDocx
from aegis.ingestion.models import ExtractedDocument

from .models import Chunk, ChunkConfig, ChunkFragment, ChunkMapping, TextUnit
from .units import iter_units


def _clip(mapping: ChunkMapping, start: int, end: int, offset: int) -> ChunkMapping | None:
    left, right = max(start, mapping.output_start), min(end, mapping.output_end)
    if left >= right:
        return None
    sources = mapping.sources
    if mapping.kind == "copy":
        sources = tuple(
            replace(
                source,
                start=source.start + left - mapping.output_start,
                end=source.start + right - mapping.output_start,
            )
            if source.end - source.start == mapping.output_end - mapping.output_start
            else source
            for source in sources
        )
    # Transformed text (e.g. a ligature) retains its entire original source range.
    return ChunkMapping(offset + left - start, offset + right - start, sources, mapping.kind)


def _cut(text: str, start: int, budget: int) -> int:
    end = min(len(text), start + budget)
    if end == len(text):
        return end
    window = text[start:end]
    lower = len(window) // 2
    sentences = list(re.finditer(r"[.!?](?:\s+)|\n", window))
    candidates = [match.end() for match in sentences if match.end() >= lower]
    if not candidates:
        candidates = [match.end() for match in re.finditer(r"\s+", window) if match.end() >= lower]
    return start + candidates[-1] if candidates else end


def iter_chunks(document: ExtractedDocument | ExtractedDocx, config: ChunkConfig | None = None):
    """Pack compatible units; overlap only within an oversized structural unit.

    PDF pages, extraction methods, heading sections, and DOCX tables never mix.
    Sizes are Unicode characters, not model tokens. Output order is stable.
    """
    config = config or ChunkConfig()
    pending: list[tuple[TextUnit, int, int, int]] = []
    pending_size = 0
    index = 0

    def emit(parts) -> Chunk:
        nonlocal index
        text = ""
        fragments = []
        mappings = []
        warnings = []
        for unit, start, end, overlap in parts:
            if text:
                separator = "\n" if unit.kind == "table_row" else "\n\n"
                mappings.append(
                    ChunkMapping(len(text), len(text) + len(separator), (), "separator")
                )
                text += separator
            offset = len(text)
            text += unit.text[start:end]
            fragments.append(ChunkFragment(unit.unit_id, start, end, offset, len(text), overlap))
            first_mapping = bisect_right(unit.mappings, start, key=lambda item: item.output_end)
            last_mapping = bisect_left(unit.mappings, end, key=lambda item: item.output_start)
            for mapping in unit.mappings[first_mapping:last_mapping]:
                clipped = _clip(mapping, start, end, offset)
                if clipped:
                    mappings.append(clipped)
            warnings.extend(unit.warnings)
            if start or end < len(unit.text):
                warnings.append("Oversized structural unit split; inspect fragment boundaries")
        first = parts[0][0]
        identity = {
            "document": document.document_id,
            "config": asdict(config),
            "algorithm": "structure-v2",
            "index": index,
            "text": text,
            "fragments": [asdict(fragment) for fragment in fragments],
            "mappings": [asdict(mapping) for mapping in mappings],
            "headings": [asdict(heading) for heading in first.headings],
        }
        chunk = Chunk(
            hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            document.document_id,
            document.source_path,
            index,
            text,
            "table" if first.kind == "table_row" else "text",
            first.headings,
            tuple(fragments),
            tuple(mappings),
            tuple(dict.fromkeys(warnings)),
            algorithm="structure-v2",
        )
        index += 1
        return chunk

    for unit in iter_units(document, config):
        if not unit.text.strip():
            continue
        compatible = not pending or (
            unit.group == pending[0][0].group
            and unit.headings == pending[0][0].headings
            and unit.kind != "heading"
        )
        separator_size = (1 if unit.kind == "table_row" else 2) if pending else 0
        # Keep a short heading with the start of its following oversized paragraph.
        if (
            pending
            and compatible
            and all(part[0].kind == "heading" for part in pending)
            and len(unit.text) > config.max_chars
            and config.max_chars - pending_size - separator_size >= config.max_chars // 2
        ):
            end = _cut(unit.text, 0, config.max_chars - pending_size - separator_size)
            yield emit([*pending, (unit, 0, end, 0)])
            pending, pending_size = [], 0
            start = end - min(config.overlap_chars, end // 2)
            previous_end = end
            while start < len(unit.text):
                end = _cut(unit.text, start, config.max_chars)
                yield emit([(unit, start, end, max(0, previous_end - start))])
                if end == len(unit.text):
                    break
                previous_end = end
                start = end - min(config.overlap_chars, (end - start) // 2)
            continue
        if pending and (
            not compatible or pending_size + separator_size + len(unit.text) > config.max_chars
        ):
            yield emit(pending)
            pending, pending_size = [], 0
        if len(unit.text) > config.max_chars:
            start, previous_end = 0, 0
            while start < len(unit.text):
                end = _cut(unit.text, start, config.max_chars)
                yield emit([(unit, start, end, max(0, previous_end - start))])
                if end == len(unit.text):
                    break
                previous_end = end
                start = end - min(config.overlap_chars, (end - start) // 2)
        else:
            pending_size += len(unit.text) + (
                (1 if unit.kind == "table_row" else 2) if pending else 0
            )
            pending.append((unit, 0, len(unit.text), 0))
    if pending:
        yield emit(pending)


def chunk_document(document: ExtractedDocument | ExtractedDocx, config: ChunkConfig | None = None):
    """Materialize chunks for inspection or persistence; iter_chunks streams them."""
    return tuple(iter_chunks(document, config))
