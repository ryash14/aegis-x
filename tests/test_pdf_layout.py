"""Verify text preservation and reading order independently of the local corpus."""

from pathlib import Path

import pymupdf
import pytest

from aegis.ingestion import LayoutConfig, TextBlock, ingest_pdf
from aegis.ingestion.reading_order import order_blocks


def test_column_order_and_spanning_title(tmp_path: Path) -> None:
    source = tmp_path / "columns.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        # Reverse insertion order to ensure geometry, not PDF insertion order, wins.
        page.insert_text((340, 140), "RIGHT FIRST")
        page.insert_text((340, 180), "RIGHT SECOND")
        page.insert_text((50, 140), "LEFT FIRST")
        page.insert_text((50, 180), "LEFT SECOND")
        page.insert_text((50, 60), "SPANNING HEADING " * 3)
        page.insert_text((50, 750), "SPANNING FOOTER " * 3)
        document.save(source)

    baseline = ingest_pdf(source)
    enhanced = ingest_pdf(source, include_layout=True)
    assert enhanced.document_id == baseline.document_id
    assert enhanced.pages[0].text == baseline.pages[0].text
    assert baseline.pages[0].layout is None
    layout = enhanced.pages[0].layout
    assert layout is not None
    positions = [
        layout.text.index(text)
        for text in (
            "SPANNING HEADING",
            "LEFT FIRST",
            "LEFT SECOND",
            "RIGHT FIRST",
            "RIGHT SECOND",
            "SPANNING FOOTER",
        )
    ]
    assert positions == sorted(positions)
    assert sorted(layout.reading_order) == sorted(block.block_id for block in layout.blocks)
    assert all(len(block.bbox) == 4 for block in layout.blocks)
    assert any(
        span.font and span.font_size > 0
        for block in layout.blocks
        for line in block.lines
        for span in line.spans
    )


def test_native_spans_remain_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "styled.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "Mixed formatting", fontsize=18)
        page.insert_text((50, 100), "Pressure = 42 kPa", fontsize=11)
        expected = page.get_text(
            "dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        )
        document.save(path)

    layout = ingest_pdf(path, include_layout=True).pages[0].layout
    assert layout is not None
    spans = [span for block in layout.blocks for line in block.lines for span in line.spans]
    native_spans = [
        span for block in expected["blocks"] for line in block["lines"] for span in line["spans"]
    ]
    assert [span.text for span in spans] == [span["text"] for span in native_spans]
    assert [span.bbox for span in spans] == [tuple(span["bbox"]) for span in native_spans]


def test_rotated_text_keeps_native_order(tmp_path: Path) -> None:
    source = tmp_path / "rotated.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((150, 200), "Vertical text", rotate=90)
        page.insert_text((50, 50), "Horizontal text")
        page.set_rotation(90)
        document.save(source)
    layout = ingest_pdf(source, include_layout=True).pages[0].layout
    assert layout is not None
    assert layout.reading_order == tuple(block.block_id for block in layout.blocks)
    assert any("native block order retained" in warning for warning in layout.warnings)
    assert any("Coordinates are unrotated" in warning for warning in layout.warnings)


def test_empty_and_overlapping_geometry() -> None:
    assert order_blocks((), LayoutConfig()) == ()
    blocks = (TextBlock(7, (0, 0, 100, 100), ()), TextBlock(2, (0, 0, 100, 100), ()))
    assert order_blocks(blocks, LayoutConfig()) == (2, 7)


def test_many_blocks_avoid_recursion_limits() -> None:
    blocks = tuple(TextBlock(i, (0, i * 20, 100, i * 20 + 10), ()) for i in range(1100))
    assert order_blocks(blocks, LayoutConfig()) == tuple(range(1100))


@pytest.mark.parametrize("kwargs", [{"min_column_gap_points": 0}, {"min_row_gap_points": -1}])
def test_invalid_layout_config(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positive"):
        LayoutConfig(**kwargs)
