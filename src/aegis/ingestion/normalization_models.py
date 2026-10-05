"""Normalized text with character-range mappings into native PDF text lines."""

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class NormalizationConfig:
    repair_drop_caps: bool = True
    min_drop_cap_font_ratio: float = 1.5
    max_drop_cap_gap_points: float = 3.0
    max_line_join_gap_ratio: float = 1.5
    max_line_join_indent_points: float = 36.0

    def __post_init__(self) -> None:
        if self.min_drop_cap_font_ratio <= 1 or self.max_drop_cap_gap_points < 0:
            raise ValueError("Drop-cap ratio must exceed one and gap must be nonnegative")
        if self.max_line_join_gap_ratio <= 0 or self.max_line_join_indent_points < 0:
            raise ValueError("Line-join ratio must be positive and indent must be nonnegative")


@dataclass(frozen=True, slots=True)
class SourceSlice:
    block_id: int
    line_index: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class TextMapping:
    output_start: int
    output_end: int
    sources: tuple[SourceSlice, ...]
    kind: str


@dataclass(frozen=True, slots=True)
class NormalizationChange:
    rule: str
    sources: tuple[SourceSlice, ...]
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class NormalizedBlock:
    block_id: int
    text: str
    mappings: tuple[TextMapping, ...]
    changes: tuple[NormalizationChange, ...]


@dataclass(frozen=True, slots=True)
class NormalizedPage:
    blocks: tuple[NormalizedBlock, ...]
    warnings: tuple[str, ...]
    algorithm: str = "conservative-v1"

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text)

    @property
    def mappings(self) -> tuple[TextMapping, ...]:
        """Derive page offsets, including generated block separators."""
        mappings = []
        cursor = 0
        for block in self.blocks:
            if not block.text:
                continue
            if cursor:
                mappings.append(TextMapping(cursor, cursor + 2, (), "separator"))
                cursor += 2
            mappings.extend(
                replace(
                    mapping,
                    output_start=mapping.output_start + cursor,
                    output_end=mapping.output_end + cursor,
                )
                for mapping in block.mappings
            )
            cursor += len(block.text)
        return tuple(mappings)
