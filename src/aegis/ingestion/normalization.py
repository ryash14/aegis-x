"""Conservative deterministic normalization; raw text and geometry stay immutable."""

import re
from dataclasses import dataclass

from .layout_models import PageLayout, TextBlock, TextLine
from .normalization_models import (
    NormalizationChange,
    NormalizationConfig,
    NormalizedBlock,
    NormalizedPage,
    SourceSlice,
    TextMapping,
)

_LIGATURES = dict(zip("ﬀﬁﬂﬃﬄﬅﬆ", ("ff", "fi", "fl", "ffi", "ffl", "st", "st"), strict=True))
_EDIT = re.compile("[ \\t\u00a0\u202f]+$|[\u00a0\u202f\ufb00-\ufb06\u00ad]")


@dataclass(frozen=True)
class _Piece:
    text: str
    sources: tuple[SourceSlice, ...]
    kind: str


def _source(block_id: int, index: int, start: int, end: int) -> tuple[SourceSlice, ...]:
    return (SourceSlice(block_id, index, start, end),)


def _line(
    line: TextLine, block_id: int, index: int
) -> tuple[list[_Piece], list[NormalizationChange]]:
    text = line.text
    pieces = []
    changes = []
    offset = 0
    for match in _EDIT.finditer(text):
        if match.start() > offset:
            pieces.append(
                _Piece(
                    text[offset : match.start()],
                    _source(block_id, index, offset, match.start()),
                    "copy",
                )
            )
        before = match.group()
        if match.end() == len(text) and before.isspace():
            after, rule = "", "trailing_whitespace"
        elif before in _LIGATURES:
            after, rule = _LIGATURES[before], "ligature"
        elif before in ("\u00a0", "\u202f"):
            after, rule = " ", "unicode_space"
        elif before == "\u00ad":
            after, rule = "", "soft_hyphen"
        else:
            after, rule = "", "trailing_whitespace"
        sources = _source(block_id, index, match.start(), match.end())
        changes.append(NormalizationChange(rule, sources, before, after))
        if after:
            pieces.append(_Piece(after, sources, rule))
        offset = match.end()
    if offset < len(text):
        pieces.append(_Piece(text[offset:], _source(block_id, index, offset, len(text)), "copy"))
    return pieces, changes


def _drop_cap(cap: TextLine, following: TextLine, config: NormalizationConfig) -> bool:
    # Require a large isolated capital beside the first lowercase body line.
    # Thresholds are geometric heuristics, so each repair is explicitly recorded.
    if not re.fullmatch("[A-Z]", cap.text) or not re.match("[a-z]", following.text):
        return False
    if cap.direction != (1.0, 0.0) or following.direction != (1.0, 0.0):
        return False
    if not cap.spans or not following.spans:
        return False
    body_size = max(span.font_size for span in following.spans)
    if body_size <= 0:
        return False
    ratio = max(span.font_size for span in cap.spans) / body_size
    gap = following.bbox[0] - cap.bbox[2]
    return (
        ratio >= config.min_drop_cap_font_ratio
        and -config.max_drop_cap_gap_points <= gap <= config.max_drop_cap_gap_points
        and cap.bbox[1] <= following.bbox[1] < cap.bbox[3]
        and following.bbox[3] <= cap.bbox[3]
    )


def _repairs(
    blocks: tuple[TextBlock, ...], config: NormalizationConfig
) -> dict[tuple[int, int], tuple[int, int]]:
    repairs = {}
    if not config.repair_drop_caps:
        return repairs
    for previous, following in zip(blocks, blocks[1:], strict=False):
        if (
            previous.lines
            and following.lines
            and _drop_cap(previous.lines[-1], following.lines[0], config)
        ):
            repairs[(previous.block_id, len(previous.lines) - 1)] = (following.block_id, 0)
    for block in blocks:
        for index, (previous, following) in enumerate(
            zip(block.lines, block.lines[1:], strict=False)
        ):
            if _drop_cap(previous, following, config):
                repairs[(block.block_id, index)] = (block.block_id, index + 1)
    return repairs


def normalize_layout(
    layout: PageLayout, config: NormalizationConfig | None = None
) -> NormalizedPage:
    """Map every emitted character to native text or an explicit separator.

    Offsets use Python Unicode character indices, are half-open, and refer to
    concatenated native spans within a line. Deleted text is retained in changes.
    Hard hyphens, internal spacing, line boundaries, and block boundaries remain
    unless an explicit soft-hyphen or geometrically justified drop-cap rule applies.
    """
    config = config if config is not None else NormalizationConfig()
    by_id = {block.block_id: block for block in layout.blocks}
    blocks = tuple(by_id[identifier] for identifier in layout.reading_order)
    repairs = _repairs(blocks, config)
    incoming = {target: origin for origin, target in repairs.items()}
    normalized = []
    for block in blocks:
        pieces = []
        changes = []
        previous_index = None
        for index, line in enumerate(block.lines):
            key = (block.block_id, index)
            if key in repairs:
                continue
            if previous_index is not None:
                previous_line = block.lines[previous_index]
                previous_text = previous_line.text.rstrip()
                line_height = max(
                    previous_line.bbox[3] - previous_line.bbox[1],
                    line.bbox[3] - line.bbox[1],
                )
                join = (
                    previous_index + 1 == index
                    and len(previous_text) >= 2
                    and previous_text.endswith("\u00ad")
                    and previous_text[-2].isalpha()
                    and line.text[:1].islower()
                    and line.direction == block.lines[previous_index].direction == (1.0, 0.0)
                    and 0
                    < line.bbox[1] - previous_line.bbox[1]
                    <= line_height * config.max_line_join_gap_ratio
                    and abs(line.bbox[0] - previous_line.bbox[0])
                    <= config.max_line_join_indent_points
                )
                if join:
                    changes.append(
                        NormalizationChange(
                            "soft_hyphen_line_join",
                            _source(
                                block.block_id,
                                previous_index,
                                len(block.lines[previous_index].text),
                                len(block.lines[previous_index].text),
                            ),
                            "\n",
                            "",
                        )
                    )
                else:
                    pieces.append(_Piece("\n", (), "separator"))
            if key in incoming:
                origin_id, origin_index = incoming[key]
                cap = by_id[origin_id].lines[origin_index].text
                sources = _source(origin_id, origin_index, 0, len(cap))
                pieces.append(_Piece(cap, sources, "drop_cap"))
                changes.append(NormalizationChange("drop_cap", sources, cap, cap))
            line_pieces, line_changes = _line(line, block.block_id, index)
            pieces.extend(line_pieces)
            changes.extend(line_changes)
            previous_index = index
        mappings = []
        cursor = 0
        for piece in pieces:
            mappings.append(
                TextMapping(cursor, cursor + len(piece.text), piece.sources, piece.kind)
            )
            cursor += len(piece.text)
        normalized.append(
            NormalizedBlock(
                block.block_id,
                "".join(piece.text for piece in pieces),
                tuple(mappings),
                tuple(changes),
            )
        )
    return NormalizedPage(
        tuple(normalized),
        (
            "Hard hyphens and internal spacing are preserved; headers and footers are not removed.",
            "Drop-cap repair is a geometry heuristic and must be reviewed.",
        ),
    )
