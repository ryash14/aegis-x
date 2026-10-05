"""Validate chunk coverage against immutable extraction, not generated expectations."""

from aegis.ingestion.docx import ExtractedDocx

from .units import iter_units


def validate_document(document, chunks, config):
    """Check coverage and output/source ranges against actual immutable extraction."""
    units = tuple(iter_units(document, config))
    by_id = {unit.unit_id: unit for unit in units}
    coverage = {key: [] for key, unit in by_id.items() if unit.text.strip()}
    source_text = source_texts(document)
    for chunk in chunks:
        if not 0 < len(chunk.text) <= config.max_chars:
            raise ValueError("Chunk length limit violated")
        cursor = 0
        for mapping in chunk.mappings:
            if mapping.output_start != cursor or not cursor < mapping.output_end <= len(chunk.text):
                raise ValueError("Output mappings have a gap or invalid range")
            cursor = mapping.output_end
            if not mapping.sources and mapping.kind != "separator":
                raise ValueError("Text has no source")
            for source in mapping.sources:
                key = source.xml_path or (
                    f"native_line:{source.page}:{source.block_id}:{source.line_index}"
                    if source.domain == "native_line"
                    else f"{source.domain}:{source.page}"
                )
                value = source_text[key]
                if not 0 <= source.start < source.end <= len(value):
                    raise ValueError("Source range outside original extraction")
                if (
                    mapping.kind == "copy"
                    and value[source.start : source.end]
                    != chunk.text[mapping.output_start : mapping.output_end]
                ):
                    raise ValueError("Copy mapping differs from original extraction")
        if cursor != len(chunk.text):
            raise ValueError("Incomplete chunk mapping")
        for fragment in chunk.fragments:
            unit = by_id[fragment.unit_id]
            if not (
                0 <= fragment.unit_start < fragment.unit_end <= len(unit.text)
                and 0 <= fragment.output_start < fragment.output_end <= len(chunk.text)
            ):
                raise ValueError("Chunk fragment has an invalid range")
            if (
                unit.text[fragment.unit_start : fragment.unit_end]
                != chunk.text[fragment.output_start : fragment.output_end]
            ):
                raise ValueError("Chunk fragment differs from structural unit")
            coverage[unit.unit_id].append((fragment.unit_start, fragment.unit_end))
    for key, ranges in coverage.items():
        cursor = 0
        for start, end in sorted(ranges):
            if start > cursor:
                raise ValueError("Source unit has missing text")
            cursor = max(cursor, end)
        if cursor != len(by_id[key].text):
            raise ValueError("Source unit is not completely covered")
    return {
        "units": len(coverage),
        "source_characters": sum(len(by_id[key].text) for key in coverage),
        "status": "complete",
    }


def source_texts(document):
    """Native extracted source text keyed by mapping domain and position."""
    source_text = {}
    if isinstance(document, ExtractedDocx):

        def visit(blocks):
            for block in blocks:
                if block.kind == "paragraph":
                    source_text[block.source] = block.text
                visit(block.children)

        visit(document.blocks)
    else:
        for page in document.pages:
            source_text[f"native_page_text:{page.number}"] = page.text
            if page.ocr:
                source_text[f"ocr_text:{page.number}"] = page.ocr.text
            if page.layout:
                for block in page.layout.blocks:
                    for index, line in enumerate(block.lines):
                        source_text[f"native_line:{page.number}:{block.block_id}:{index}"] = (
                            line.text
                        )
    return source_text
