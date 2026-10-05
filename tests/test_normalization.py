"""Check conservative edits, geometry guards, and recoverable source ranges."""

from pathlib import Path

import pymupdf
import pytest

from aegis.ingestion import (
    NormalizationConfig,
    PageLayout,
    TextBlock,
    TextLine,
    TextSpan,
    ingest_pdf,
    normalize_layout,
)


def line(text: str, bbox: tuple[float, float, float, float], size: float = 11) -> TextLine:
    return TextLine(bbox, (1.0, 0.0), (TextSpan(text, bbox, "TestFont", size),))


def layout_with_lines(*texts: str) -> PageLayout:
    lines = tuple(
        line(text, (10, 10 + index * 16, 200, 24 + index * 16)) for index, text in enumerate(texts)
    )
    block = TextBlock(5, (10, 10, 200, 24 + (len(texts) - 1) * 16), lines)
    return PageLayout((block,), (5,), block.text, ())


def assert_mappings(layout: PageLayout) -> None:
    normalized = normalize_layout(layout)
    by_id = {block.block_id: block for block in layout.blocks}
    for block in normalized.blocks:
        cursor = 0
        for mapping in block.mappings:
            assert mapping.output_start == cursor
            assert mapping.output_end > mapping.output_start
            cursor = mapping.output_end
            sources = []
            for source in mapping.sources:
                text = by_id[source.block_id].lines[source.line_index].text
                assert 0 <= source.start <= source.end <= len(text)
                sources.append(text[source.start : source.end])
            if mapping.kind in {"copy", "drop_cap"}:
                assert block.text[mapping.output_start : mapping.output_end] == "".join(sources)
            if mapping.kind == "separator":
                assert not mapping.sources
        assert cursor == len(block.text)


def test_discretionary_hyphens_and_ligatures() -> None:
    source = layout_with_lines("An efﬁcient engineer\u00ad", "ing process\u00a0works.  ")
    result = normalize_layout(source)
    assert result.text == "An efficient engineering process works."
    assert {change.rule for change in result.blocks[0].changes} == {
        "ligature",
        "soft_hyphen",
        "soft_hyphen_line_join",
        "unicode_space",
        "trailing_whitespace",
    }
    assert source.blocks[0].lines[0].text == "An efﬁcient engineer\u00ad"
    assert normalize_layout(source) == result
    assert_mappings(source)


def test_preserve_hard_hyphens_units_and_internal_alignment() -> None:
    source = layout_with_lines("  x  =  2\t+  3", "non-", "linear system: 2² m³; −5 K")
    result = normalize_layout(source)
    assert result.text == "  x  =  2\t+  3\nnon-\nlinear system: 2² m³; −5 K"
    assert not result.blocks[0].changes
    assert_mappings(source)


def test_hyphen_does_not_join_across_blocks() -> None:
    first = TextBlock(4, (10, 10, 100, 24), (line("engineer\u00ad", (10, 10, 100, 24)),))
    second = TextBlock(8, (10, 50, 100, 64), (line("ing", (10, 50, 100, 64)),))
    source = PageLayout((first, second), (4, 8), "", ())
    assert normalize_layout(source).text == "engineer\n\ning"
    assert_mappings(source)


def drop_cap_layout(*, size: float = 30, gap: float = 0, body: str = "his handbook") -> PageLayout:
    heading = line("1.1 Purpose", (50, 60, 200, 75))
    cap = line("T", (50, 80, 70, 125), size)
    body_line = line(body, (70 + gap, 90, 240, 105))
    first = TextBlock(1, (50, 60, 200, 125), (heading, cap))
    second = TextBlock(2, body_line.bbox, (body_line,))
    return PageLayout((first, second), (1, 2), first.text + "\n\n" + second.text, ())


def test_drop_cap_repair_maps_both_original_blocks() -> None:
    source = drop_cap_layout()
    result = normalize_layout(source)
    assert result.text == "1.1 Purpose\n\nThis handbook"
    assert result.blocks[1].changes[0].rule == "drop_cap"
    assert result.blocks[1].mappings[0].sources[0].block_id == 1
    assert result.blocks[1].mappings[1].sources[0].block_id == 2
    assert source.blocks[0].lines[-1].text == "T"
    assert_mappings(source)
    assert normalize_layout(source, NormalizationConfig(repair_drop_caps=False)).text == source.text


@pytest.mark.parametrize("kwargs", [{"size": 11}, {"gap": 20}, {"body": "UPPERCASE"}, {"size": 0}])
def test_drop_cap_guards(kwargs: dict) -> None:
    source = drop_cap_layout(**kwargs)
    assert normalize_layout(source).text == source.text


@pytest.mark.parametrize("text", ["abc\u00a0", "abc\u202f", "abc \t", "  \t"])
def test_trailing_whitespace(text: str) -> None:
    result = normalize_layout(layout_with_lines(text))
    assert result.text == text.rstrip()


def test_pdf_normalization_is_opt_in_and_implies_layout(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Local technical report")
        document.save(source)
    baseline = ingest_pdf(source)
    normalized = ingest_pdf(source, include_normalized=True)
    assert baseline.pages[0].normalized is None
    assert normalized.pages[0].layout is not None
    assert normalized.pages[0].normalized is not None
    assert normalized.pages[0].text == baseline.pages[0].text
    assert normalized.document_id == baseline.document_id
    assert normalized.pages[0].normalized.text == "Local technical report"


def test_empty_layout() -> None:
    assert normalize_layout(PageLayout((), (), "", ())).text == ""


def test_page_mapping_covers_block_separators() -> None:
    result = normalize_layout(drop_cap_layout())
    cursor = 0
    for mapping in result.mappings:
        assert mapping.output_start == cursor
        cursor = mapping.output_end
        if mapping.kind == "separator":
            assert not mapping.sources
    assert cursor == len(result.text)


@pytest.mark.parametrize("bbox", [(10, 100, 200, 114), (300, 26, 450, 40), (10, 10, 200, 24)])
def test_soft_hyphen_does_not_join_distant_or_parallel_lines(bbox: tuple) -> None:
    first = line("engineer\u00ad", (10, 10, 200, 24))
    second = line("ing", bbox)
    block = TextBlock(1, (10, 10, 450, 114), (first, second))
    source = PageLayout((block,), (1,), block.text, ())
    assert normalize_layout(source).text == "engineer\ning"


@pytest.mark.parametrize(
    "kwargs", [{"min_drop_cap_font_ratio": 1}, {"max_drop_cap_gap_points": -1}]
)
def test_config_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        NormalizationConfig(**kwargs)
