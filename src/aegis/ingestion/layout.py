"""Preserve native text blocks and derive an inspectable reading order."""

import pymupdf

from .layout_models import LayoutConfig, PageLayout, TextBlock, TextLine, TextSpan
from .reading_order import order_blocks


def extract_layout(page: pymupdf.Page, config: LayoutConfig) -> PageLayout:
    # Keep image binaries out of text geometry to avoid duplicating large scans.
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    native = page.get_text("dict", flags=flags, sort=False)
    blocks = tuple(
        TextBlock(
            block_id=block["number"],
            bbox=tuple(block["bbox"]),
            lines=tuple(
                TextLine(
                    bbox=tuple(line["bbox"]),
                    direction=tuple(line["dir"]),
                    spans=tuple(
                        TextSpan(span["text"], tuple(span["bbox"]), span["font"], span["size"])
                        for span in line["spans"]
                    ),
                )
                for line in block["lines"]
            ),
        )
        for block in native["blocks"]
        if block["type"] == 0
    )
    warnings = [
        "Geometric reading order is heuristic; table and equation structure is not inferred."
    ]
    nonhorizontal = any(
        abs(line.direction[0] - 1) > 0.01 or abs(line.direction[1]) > 0.01
        for block in blocks
        for line in block.lines
    )
    if nonhorizontal:
        warnings.append("Rotated or nonhorizontal text: native block order retained.")
        order = tuple(block.block_id for block in blocks)
    else:
        order = order_blocks(blocks, config)
    if page.rotation:
        warnings.append("Coordinates are unrotated; rotate overlays to match the displayed page.")
    by_id = {block.block_id: block for block in blocks}
    return PageLayout(
        blocks=blocks,
        reading_order=order,
        text="\n\n".join(by_id[identifier].text for identifier in order),
        warnings=tuple(warnings),
    )
