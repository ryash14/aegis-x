"""Conservative recursive XY cuts for horizontal, left-to-right text blocks."""

from .layout_models import LayoutConfig, TextBlock


def _largest_gap(blocks: tuple[TextBlock, ...], axis: int, minimum: float) -> float | None:
    intervals = sorted((block.bbox[axis], block.bbox[axis + 2]) for block in blocks)
    end = intervals[0][1]
    largest = 0.0
    cut = None
    for start, stop in intervals[1:]:
        gap = start - end
        if gap >= minimum and gap > largest:
            largest = gap
            cut = (start + end) / 2
        end = max(end, stop)
    return cut


def order_blocks(blocks: tuple[TextBlock, ...], config: LayoutConfig) -> tuple[int, ...]:
    """Prefer column cuts; spanning headings force row cuts first.

    This preserves each native block and line rather than fabricating paragraphs.
    Dense/overlapping layouts fall back to top-to-bottom ordering.
    An explicit stack avoids recursion limits on documents with many tiny blocks.
    """
    pending = [blocks]
    ordered = []
    while pending:
        group = pending.pop()
        if not group:
            continue
        split = None
        for axis, minimum in ((0, config.min_column_gap_points), (1, config.min_row_gap_points)):
            cut = _largest_gap(group, axis, minimum)
            if cut is not None:
                before = tuple(block for block in group if block.bbox[axis + 2] <= cut)
                after = tuple(block for block in group if block.bbox[axis] > cut)
                if before and after and len(before) + len(after) == len(group):
                    split = (before, after)
                    break
        if split:
            pending.extend((split[1], split[0]))
        else:
            ordered.extend(
                block.block_id
                for block in sorted(
                    group, key=lambda block: (block.bbox[1], block.bbox[0], block.block_id)
                )
            )
    return tuple(ordered)
