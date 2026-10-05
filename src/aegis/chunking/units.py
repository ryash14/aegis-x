"""Adapt extracted PDF and DOCX records into ordered, traceable structural units."""

from collections import Counter
from itertools import groupby

from aegis.ingestion.docx import DocxBlock, ExtractedDocx
from aegis.ingestion.models import ExtractedDocument

from .models import ChunkConfig, ChunkHeading, ChunkMapping, ChunkSource, TextUnit


def _pdf_units(document: ExtractedDocument, config: ChunkConfig):
    headings = ()
    for page in document.pages:
        base_warnings = list(page.warnings)
        if page.normalized and page.normalized.text:
            blocks = {block.block_id: block for block in page.layout.blocks} if page.layout else {}
            fonts = Counter()
            for block in blocks.values():
                for line in block.lines:
                    for span in line.spans:
                        if span.text.strip():
                            fonts[round(span.font_size, 1)] += len(span.text.strip())
            body_size = fonts.most_common(1)[0][0] if fonts else 0
            for block in page.normalized.blocks:
                if not block.text:
                    continue
                mappings = []
                for mapping in block.mappings:
                    sources = []
                    for origin in mapping.sources:
                        native = blocks.get(origin.block_id)
                        bbox = native.lines[origin.line_index].bbox if native else None
                        sources.append(
                            ChunkSource(
                                "native_line",
                                origin.start,
                                origin.end,
                                page.number,
                                origin.block_id,
                                origin.line_index,
                                bbox=bbox,
                            )
                        )
                    mappings.append(
                        ChunkMapping(
                            mapping.output_start, mapping.output_end, tuple(sources), mapping.kind
                        )
                    )
                warnings = [*base_warnings, *page.normalized.warnings]
                native = blocks.get(block.block_id)
                is_heading = False
                if config.infer_pdf_headings and native and body_size and mappings:
                    spans = [
                        span for line in native.lines for span in line.spans if span.text.strip()
                    ]
                    total = sum(len(span.text.strip()) for span in spans)
                    large = sum(
                        len(span.text.strip())
                        for span in spans
                        if span.font_size >= body_size * config.heading_font_ratio
                    )
                    is_heading = (
                        len(block.text) <= config.max_heading_chars
                        and total > 0
                        and large / total >= 0.8
                        and not block.text.rstrip().endswith((".", ";", ","))
                    )
                if is_heading:
                    origins = [source for mapping in mappings for source in mapping.sources]
                    if origins:
                        headings = (ChunkHeading(block.text, 1, "pdf-font-heuristic", origins[0]),)
                if headings and headings[0].method == "pdf-font-heuristic":
                    warnings.append("PDF heading is a font heuristic; hierarchy is unverified")
                yield TextUnit(
                    f"pdf:{page.number}:normalized:{block.block_id}",
                    block.text,
                    "heading" if is_heading else "paragraph",
                    f"pdf:{page.number}:normalized",
                    headings,
                    tuple(mappings),
                    tuple(dict.fromkeys(warnings)),
                )
        elif page.layout and page.layout.blocks:
            blocks = {block.block_id: block for block in page.layout.blocks}
            for identifier in page.layout.reading_order:
                block = blocks[identifier]
                text = ""
                mappings = []
                for index, line in enumerate(block.lines):
                    if index:
                        mappings.append(ChunkMapping(len(text), len(text) + 1, (), "separator"))
                        text += "\n"
                    start = len(text)
                    text += line.text
                    if line.text:
                        mappings.append(
                            ChunkMapping(
                                start,
                                len(text),
                                (
                                    ChunkSource(
                                        "native_line",
                                        0,
                                        len(line.text),
                                        page.number,
                                        identifier,
                                        index,
                                        bbox=line.bbox,
                                    ),
                                ),
                                "copy",
                            )
                        )
                if text.strip():
                    yield TextUnit(
                        f"pdf:{page.number}:layout:{identifier}",
                        text,
                        "paragraph",
                        f"pdf:{page.number}:layout",
                        headings,
                        tuple(mappings),
                        tuple([*base_warnings, *page.layout.warnings]),
                    )
        elif page.text.strip():
            yield TextUnit(
                f"pdf:{page.number}:baseline",
                page.text,
                "page",
                f"pdf:{page.number}:baseline",
                headings,
                (
                    ChunkMapping(
                        0,
                        len(page.text),
                        (ChunkSource("native_page_text", 0, len(page.text), page.number),),
                        "copy",
                    ),
                ),
                tuple([*base_warnings, "Baseline fallback lacks block geometry"]),
            )
        elif page.ocr and page.ocr.text.strip():
            for key, items in groupby(
                page.ocr.words, key=lambda word: (word.block, word.paragraph)
            ):
                words = list(items)
                start, end = words[0].output_start, words[-1].output_end
                mappings = []
                cursor = start
                for word in words:
                    if word.output_start > cursor:
                        mappings.append(
                            ChunkMapping(cursor - start, word.output_start - start, (), "separator")
                        )
                    mappings.append(
                        ChunkMapping(
                            word.output_start - start,
                            word.output_end - start,
                            (
                                ChunkSource(
                                    "ocr_text",
                                    word.output_start,
                                    word.output_end,
                                    page.number,
                                    word.block,
                                    word.line,
                                    bbox=word.bbox,
                                    confidence=word.confidence,
                                ),
                            ),
                            "copy",
                        )
                    )
                    cursor = word.output_end
                yield TextUnit(
                    f"pdf:{page.number}:ocr:{key[0]}:{key[1]}",
                    page.ocr.text[start:end],
                    "paragraph",
                    f"pdf:{page.number}:ocr",
                    (),
                    tuple(mappings),
                    tuple([*base_warnings, *page.ocr.warnings]),
                )


def _paragraph(block: DocxBlock):
    if block.text:
        yield block.text, ChunkSource("docx_paragraph", 0, len(block.text), xml_path=block.source)


def _cell_items(block: DocxBlock):
    for child in block.children:
        if child.kind == "paragraph":
            yield from _paragraph(child)
        elif child.kind == "table":
            for row in child.children:
                for cell in row.children:
                    yield from _cell_items(cell)


def _docx_units(document: ExtractedDocx):
    headings = []
    for block in document.blocks:
        if block.kind == "paragraph" and block.text.strip():
            source = ChunkSource("docx_paragraph", 0, len(block.text), xml_path=block.source)
            if block.heading_level:
                headings = [heading for heading in headings if heading.level < block.heading_level]
                headings.append(
                    ChunkHeading(block.text, block.heading_level, "docx-outline", source)
                )
            yield TextUnit(
                block.source,
                block.text,
                "heading" if block.heading_level else "paragraph",
                "docx-body",
                tuple(headings),
                (ChunkMapping(0, len(block.text), (source,), "copy"),),
                document.warnings,
            )
        elif block.kind == "table":
            for row in block.children:
                text = ""
                mappings = []
                warnings = list(document.warnings)
                for index, cell in enumerate(row.children):
                    if index:
                        mappings.append(ChunkMapping(len(text), len(text) + 1, (), "separator"))
                        text += "\t"
                    if cell.vertical_merge or cell.column_span != 1:
                        warnings.append(
                            "Merged table cells retain source markers; grid not expanded"
                        )
                    if any(child.kind == "table" for child in cell.children):
                        warnings.append("Nested table text flattened within its parent cell")
                    for paragraph_index, (value, source) in enumerate(_cell_items(cell)):
                        if paragraph_index:
                            mappings.append(ChunkMapping(len(text), len(text) + 1, (), "separator"))
                            text += "\n"
                        start = len(text)
                        text += value
                        mappings.append(ChunkMapping(start, len(text), (source,), "copy"))
                if text.strip():
                    yield TextUnit(
                        row.source,
                        text,
                        "table_row",
                        block.source,
                        tuple(headings),
                        tuple(mappings),
                        tuple(dict.fromkeys(warnings)),
                    )


def iter_units(document: ExtractedDocument | ExtractedDocx, config: ChunkConfig | None = None):
    """Yield structural units in source order without copying the full document text."""
    config = config or ChunkConfig()
    if isinstance(document, ExtractedDocument):
        yield from _pdf_units(document, config)
    elif isinstance(document, ExtractedDocx):
        yield from _docx_units(document)
    else:
        raise TypeError("Chunking requires an extracted PDF or DOCX record")
