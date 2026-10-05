"""Verify complete text coverage, boundaries, deterministic IDs, and source mappings."""

from dataclasses import replace

import pymupdf
import pytest

from aegis.chunking import ChunkConfig, chunk_document, iter_chunks, iter_units
from aegis.ingestion import ingest_pdf, normalize_layout
from aegis.ingestion.docx import DocxBlock, ExtractedDocx
from aegis.ingestion.layout_models import PageLayout, TextBlock, TextLine, TextSpan
from aegis.ingestion.models import DocumentMetadata, ExtractedDocument, ExtractedPage, PageStatus
from aegis.ingestion.ocr_models import OCRConfig, OCREngine, OCRPage, OCRWord


def docx(blocks):
    return ExtractedDocx("doc-hash", "/source.docx", 100, tuple(blocks), (), ())


def paragraph(text, index, level=None):
    return DocxBlock("paragraph", f"word/document.xml/body/p[{index}]", text, level)


def verify_coverage(document, config):
    units = {unit.unit_id: unit for unit in iter_units(document, config)}
    chunks = chunk_document(document, config)
    covered = {key: bytearray(len(unit.text)) for key, unit in units.items() if unit.text.strip()}
    for chunk in chunks:
        assert 0 < len(chunk.text) <= config.max_chars
        cursor = 0
        for mapping in chunk.mappings:
            assert mapping.output_start == cursor
            assert mapping.output_end > cursor
            cursor = mapping.output_end
            assert mapping.sources or mapping.kind == "separator"
            for source in mapping.sources:
                assert 0 <= source.start < source.end
        assert cursor == len(chunk.text)
        for fragment in chunk.fragments:
            unit = units[fragment.unit_id]
            assert (
                chunk.text[fragment.output_start : fragment.output_end]
                == unit.text[fragment.unit_start : fragment.unit_end]
            )
            covered[unit.unit_id][fragment.unit_start : fragment.unit_end] = b"\x01" * (
                fragment.unit_end - fragment.unit_start
            )
            assert 0 <= fragment.overlap_prefix_chars <= config.overlap_chars
    assert all(all(value) for value in covered.values())
    return chunks


def test_docx_sections_and_table_boundaries():
    table = DocxBlock(
        "table",
        "table[0]",
        children=(
            DocxBlock(
                "row",
                "table[0]/row[0]",
                children=(
                    DocxBlock("cell", "cell[0]", children=(paragraph("Pressure", 2),)),
                    DocxBlock("cell", "cell[1]", children=(paragraph("42 kPa", 3),)),
                ),
            ),
        ),
    )
    document = docx(
        [
            paragraph("Propulsion", 0, 1),
            paragraph("Engine context", 1),
            table,
            paragraph("Thermal", 4, 1),
            paragraph("Temperature stable", 5),
        ]
    )
    chunks = verify_coverage(document, ChunkConfig())
    assert len(chunks) == 3
    assert chunks[0].text == "Propulsion\n\nEngine context"
    assert chunks[1].kind == "table" and chunks[1].text == "Pressure\t42 kPa"
    assert chunks[1].headings[0].text == "Propulsion"
    assert chunks[2].headings[0].text == "Thermal"
    assert all(source.xml_path for mapping in chunks[1].mappings for source in mapping.sources)


def test_nested_heading_stack():
    document = docx(
        [paragraph("A", 0, 1), paragraph("B", 1, 2), paragraph("C", 2, 2), paragraph("D", 3, 1)]
    )
    chunks = chunk_document(document)
    assert [[heading.text for heading in chunk.headings] for chunk in chunks] == [
        ["A"],
        ["A", "B"],
        ["A", "C"],
        ["D"],
    ]


@pytest.mark.parametrize("text", ["alpha beta gamma. " * 100, "X" * 2000, "界🙂" * 600])
@pytest.mark.parametrize("size,overlap", [(100, 20), (1, 0), (40, 39)])
def test_oversized_units_keep_all_characters(text, size, overlap):
    document = docx([paragraph(text, 0)])
    config = ChunkConfig(max_chars=size, overlap_chars=overlap)
    chunks = verify_coverage(document, config)
    assert len(chunks) > 1
    assert any("Oversized" in warning for warning in chunks[0].warnings)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert chunks == tuple(iter_chunks(document, config))
    assert [chunk.chunk_id for chunk in chunks] == [
        chunk.chunk_id for chunk in chunk_document(document, config)
    ]
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_exact_fit_and_paragraph_separator_budget():
    document = docx([paragraph("A" * 10, 0), paragraph("B" * 10, 1)])
    assert len(verify_coverage(document, ChunkConfig(max_chars=22, overlap_chars=0))) == 1
    assert len(verify_coverage(document, ChunkConfig(max_chars=21, overlap_chars=0))) == 2


def test_identity_independent_of_source_path_but_sensitive_to_settings():
    document = docx([paragraph("Stable content", 0)])
    first = chunk_document(document)[0]
    moved = chunk_document(replace(document, source_path="/copy.docx"))[0]
    assert first.chunk_id == moved.chunk_id and first.source_path != moved.source_path
    assert first.chunk_id != chunk_document(document, ChunkConfig(max_chars=2000))[0].chunk_id


def pdf_record(pages):
    return ExtractedDocument(
        "pdf-hash",
        "/source.pdf",
        100,
        DocumentMetadata("", "", "", "", "", "", ""),
        tuple(pages),
        "test",
        False,
    )


def test_pdf_page_boundaries_and_baseline_offsets():
    document = pdf_record(
        [
            ExtractedPage(1, "First page", 500, 600, PageStatus.TEXT),
            ExtractedPage(2, "Second page", 500, 600, PageStatus.TEXT),
        ]
    )
    chunks = verify_coverage(document, ChunkConfig())
    assert len(chunks) == 2
    assert [chunk.mappings[0].sources[0].page for chunk in chunks] == [1, 2]
    assert chunks[0].mappings[0].sources[0].domain == "native_page_text"


def test_normalized_ligature_clipping_keeps_original_source():
    bbox = (0, 0, 100, 20)
    line = TextLine(bbox, (1, 0), (TextSpan("ﬃabc", bbox, "font", 10),))
    layout = PageLayout((TextBlock(0, bbox, (line,)),), (0,), line.text, ())
    page = ExtractedPage(1, line.text, 500, 600, PageStatus.TEXT, layout, normalize_layout(layout))
    chunks = verify_coverage(pdf_record([page]), ChunkConfig(max_chars=2, overlap_chars=0))
    assert chunks[0].text == "ff" and chunks[1].text == "ia"
    assert chunks[0].mappings[0].sources[0].start == 0
    assert chunks[0].mappings[0].sources[0].end == 1
    assert chunks[1].mappings[0].sources[0].end == 1
    assert chunks[1].mappings[1].sources[0].start == 1


def test_pdf_layout_fallback_preserves_line_positions():
    bbox = (0, 0, 100, 20)
    line = TextLine(bbox, (1, 0), (TextSpan("Native", bbox, "font", 10),))
    layout = PageLayout((TextBlock(7, bbox, (line, line)),), (7,), "Native\nNative", ())
    page = ExtractedPage(1, "Native", 500, 600, PageStatus.TEXT, layout)
    chunk = verify_coverage(pdf_record([page]), ChunkConfig())[0]
    assert chunk.text == "Native\nNative"
    assert chunk.mappings[2].sources[0].line_index == 1


def test_ocr_coordinates_confidence_and_word_offsets():
    bbox = (10, 20, 30, 40)
    words = (
        OCRWord("Alpha", bbox, 90, 1, 1, 1, 1, 0, 5),
        OCRWord("Beta", bbox, 60, 1, 1, 1, 2, 6, 10),
    )
    ocr = OCRPage(
        "Alpha Beta",
        words,
        OCREngine("test", "hash", ()),
        OCRConfig(),
        0,
        10,
        500,
        600,
        ("Review OCR",),
        0,
    )
    page = ExtractedPage(1, "", 500, 600, PageStatus.IMAGE_ONLY, ocr=ocr)
    chunk = verify_coverage(pdf_record([page]), ChunkConfig())[0]
    assert chunk.mappings[2].sources[0].confidence == 60
    assert chunk.mappings[2].sources[0].start == 6
    assert chunk.mappings[2].sources[0].bbox == bbox
    assert "Review OCR" in chunk.warnings


def test_real_pdf_heading_heuristic_and_source_positions(tmp_path):
    path = tmp_path / "headings.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), "Engine section", fontsize=22)
        page.insert_textbox(
            pymupdf.Rect(40, 100, 500, 400), "Operating pressure remains stable. " * 10, fontsize=11
        )
        document.save(path)
    extracted = ingest_pdf(path, include_normalized=True)
    chunks = verify_coverage(extracted, ChunkConfig())
    assert chunks[0].headings[0].text == "Engine section"
    assert chunks[0].headings[0].method == "pdf-font-heuristic"
    assert chunks[0].mappings[0].sources[0].bbox
    disabled = chunk_document(extracted, ChunkConfig(infer_pdf_headings=False))
    assert not disabled[0].headings


def test_oversized_table_row_and_nested_table_sources():
    nested = DocxBlock(
        "table",
        "nested",
        children=(
            DocxBlock(
                "row",
                "nested/row",
                children=(
                    DocxBlock(
                        "cell", "nested/cell", children=(paragraph("Nested content " * 20, 9),)
                    ),
                ),
            ),
        ),
    )
    table = DocxBlock(
        "table",
        "table",
        children=(
            DocxBlock(
                "row",
                "table/row",
                children=(DocxBlock("cell", "cell", children=(nested,), column_span=2),),
            ),
        ),
    )
    chunks = verify_coverage(docx([table]), ChunkConfig(max_chars=80, overlap_chars=10))
    assert all(chunk.kind == "table" for chunk in chunks)
    assert any("Nested table" in warning for warning in chunks[0].warnings)
    assert chunks[0].mappings[0].sources[0].xml_path.endswith("p[9]")


def test_empty_documents_and_invalid_input():
    assert not chunk_document(docx([]))
    assert not chunk_document(docx([paragraph("", 0)]))
    with pytest.raises(TypeError):
        chunk_document("not an extracted document")


@pytest.mark.parametrize(
    "settings",
    [
        {"max_chars": 0},
        {"overlap_chars": -1},
        {"max_chars": 10, "overlap_chars": 10},
        {"heading_font_ratio": float("nan")},
        {"max_heading_chars": 0},
    ],
)
def test_invalid_chunk_settings(settings):
    with pytest.raises(ValueError):
        ChunkConfig(**settings)
