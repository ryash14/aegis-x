"""Text geometry in PDF points, in the page's unrotated coordinate system."""

from dataclasses import dataclass

BoundingBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class LayoutConfig:
    min_column_gap_points: float = 10.0
    min_row_gap_points: float = 6.0

    def __post_init__(self) -> None:
        if self.min_column_gap_points <= 0 or self.min_row_gap_points <= 0:
            raise ValueError("Layout gaps must be positive")


@dataclass(frozen=True, slots=True)
class TextSpan:
    text: str
    bbox: BoundingBox
    font: str
    font_size: float


@dataclass(frozen=True, slots=True)
class TextLine:
    bbox: BoundingBox
    direction: tuple[float, float]
    spans: tuple[TextSpan, ...]

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


@dataclass(frozen=True, slots=True)
class TextBlock:
    block_id: int
    bbox: BoundingBox
    lines: tuple[TextLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class PageLayout:
    blocks: tuple[TextBlock, ...]
    reading_order: tuple[int, ...]
    text: str
    warnings: tuple[str, ...]
    algorithm: str = "xy-cut-v1"
