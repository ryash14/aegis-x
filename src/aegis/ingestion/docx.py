"""Bounded, offline OOXML extraction; source positions refer to XML, not pages."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .errors import ErrorCode, IngestionError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


@dataclass(frozen=True, slots=True)
class DocxLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_members: int = 10000

    def __post_init__(self) -> None:
        if min(self.max_file_bytes, self.max_uncompressed_bytes, self.max_members) <= 0:
            raise ValueError("DOCX limits must be positive")


@dataclass(frozen=True, slots=True)
class DocxBlock:
    kind: str
    source: str
    text: str = ""
    heading_level: int | None = None
    children: tuple["DocxBlock", ...] = ()
    column_span: int = 1
    vertical_merge: str | None = None
    image_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DocxImage:
    relationship_id: str
    target: str
    external: bool
    sha256: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class ExtractedDocx:
    document_id: str
    source_path: str
    size_bytes: int
    blocks: tuple[DocxBlock, ...]
    images: tuple[DocxImage, ...]
    warnings: tuple[str, ...]
    parser_version: str = "ooxml-v1"


def ingest_docx(source: str | Path, *, limits: DocxLimits | None = None) -> ExtractedDocx:
    """Extract paragraphs, recursive tables, headings, equations and image references."""
    limits = limits or DocxLimits()
    path = Path(source).absolute()
    try:
        with path.open("rb") as stream:
            snapshot = stream.read(limits.max_file_bytes + 1)
    except OSError as exc:
        raise IngestionError(ErrorCode.SOURCE_UNREADABLE, "Cannot read DOCX source") from exc
    if len(snapshot) > limits.max_file_bytes:
        raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "DOCX file limit exceeded")
    warnings: set[str] = set()
    try:
        with ZipFile(BytesIO(snapshot)) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if (
                len(members) > limits.max_members
                or sum(item.file_size for item in members) > limits.max_uncompressed_bytes
            ):
                raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "DOCX archive limit exceeded")
            if len(names) != len(set(names)) or any(item.flag_bits & 1 for item in members):
                raise ValueError("Ambiguous or encrypted archive")

            def xml(name: str) -> ET.Element:
                raw = archive.read(name)
                # OOXML does not need DTDs; reject declarations before XML parsing,
                # including UTF-16/32 encoded declarations.
                probe = raw.replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                    raise ValueError("XML declarations forbidden")
                return ET.fromstring(raw)

            root = xml("word/document.xml")
            if root.tag != W + "document":
                raise ValueError("Unsupported Word namespace")
            body = root.find(W + "body")
            if body is None:
                raise ValueError("Missing document body")
            styles = {}
            if "word/styles.xml" in names:
                for style in xml("word/styles.xml").findall(W + "style"):
                    outline = style.find(f"{W}pPr/{W}outlineLvl")
                    parent = style.find(W + "basedOn")
                    styles[style.get(W + "styleId")] = (
                        outline.get(W + "val") if outline is not None else None,
                        parent.get(W + "val") if parent is not None else None,
                    )

            def heading(node: ET.Element) -> int | None:
                direct = node.find(f"{W}pPr/{W}outlineLvl")
                value = direct.get(W + "val") if direct is not None else None
                style = node.find(f"{W}pPr/{W}pStyle")
                key = style.get(W + "val") if style is not None else None
                seen = set()
                while value is None and key in styles and key not in seen:
                    seen.add(key)
                    value, key = styles[key]
                return (
                    int(value) + 1
                    if value is not None and value.isdigit() and int(value) < 9
                    else None
                )

            def text(node: ET.Element) -> str:
                if node.tag == W + "del":
                    warnings.add("Deleted tracked text excluded; inserted text retained")
                    return ""
                if node.tag in (W + "t", M + "t"):
                    return node.text or ""
                if node.tag == W + "tab":
                    return "\t"
                if node.tag in (W + "br", W + "cr"):
                    return "\n"
                if node.tag == W + "noBreakHyphen":
                    return "‑"
                if node.tag == W + "softHyphen":
                    return "\u00ad"
                return "".join(text(child) for child in node)

            def blocks(node: ET.Element, location: str) -> tuple[DocxBlock, ...]:
                result = []
                for index, child in enumerate(node):
                    tag = child.tag.split("}")[-1]
                    position = f"{location}/{tag}[{index}]"
                    if child.tag == W + "p":
                        image_ids = tuple(
                            dict.fromkeys(
                                e.get(R + "embed") or e.get(R + "link")
                                for e in child.iter(A + "blip")
                                if e.get(R + "embed") or e.get(R + "link")
                            )
                        )
                        result.append(
                            DocxBlock(
                                "paragraph",
                                position,
                                text(child),
                                heading(child),
                                image_ids=image_ids,
                            )
                        )
                    elif child.tag in (W + "tbl", W + "tr", W + "tc"):
                        kind = {W + "tbl": "table", W + "tr": "row", W + "tc": "cell"}[child.tag]
                        span = child.find(f"{W}tcPr/{W}gridSpan")
                        merge = child.find(f"{W}tcPr/{W}vMerge")
                        result.append(
                            DocxBlock(
                                kind,
                                position,
                                children=blocks(child, position),
                                column_span=int(span.get(W + "val", "1"))
                                if span is not None
                                else 1,
                                vertical_merge=merge.get(W + "val", "continue")
                                if merge is not None
                                else None,
                            )
                        )
                    elif child.tag in (W + "sdt", W + "sdtContent", W + "ins", W + "customXml"):
                        result.extend(blocks(child, position))
                    elif child.tag == W + "altChunk":
                        warnings.add("Embedded alternate-format content not extracted")
                return tuple(result)

            images = []
            relpath = "word/_rels/document.xml.rels"
            if relpath in names:
                import posixpath

                for rel in xml(relpath):
                    if not rel.get("Type", "").endswith("/image"):
                        continue
                    target = rel.get("Target", "")
                    external = rel.get("TargetMode") == "External"
                    digest = None
                    size = None
                    if not external:
                        member = posixpath.normpath(posixpath.join("word", target))
                        if member.startswith("../") or member.startswith("/"):
                            warnings.add("Invalid image target not read")
                        elif member in names:
                            payload = archive.read(member)
                            digest, size = sha256(payload).hexdigest(), len(payload)
                        else:
                            warnings.add("Missing embedded image")
                    else:
                        warnings.add("External image references retained; never fetched")
                    images.append(DocxImage(rel.get("Id", ""), target, external, digest, size))
            if any(e.tag.startswith(M) for e in root.iter()):
                warnings.add("Equations preserved as text tokens; mathematical layout unavailable")
            if any(n.startswith(("word/header", "word/footer")) for n in names):
                warnings.add("Headers and footers excluded from document body")
            if any(e.tag == W + "drawing" for e in root.iter()):
                warnings.add("Images referenced without rendering or OCR")
            result = blocks(body, "word/document.xml/body")
            return ExtractedDocx(
                sha256(snapshot).hexdigest(),
                str(path),
                len(snapshot),
                result,
                tuple(images),
                tuple(sorted(warnings)),
            )
    except IngestionError:
        raise
    except (
        BadZipFile,
        KeyError,
        ValueError,
        ET.ParseError,
        RuntimeError,
        OSError,
        RecursionError,
        NotImplementedError,
    ) as exc:
        raise IngestionError(ErrorCode.INVALID_DOCX, "Invalid or unsupported DOCX package") from exc
