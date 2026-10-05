from hashlib import sha256
from zipfile import ZipFile

import pytest

from aegis.ingestion import DocxLimits, ErrorCode, IngestionError, ingest_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def package(tmp_path, body, extra=None):
    path = tmp_path / "sample.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        )
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return path


def test_structure_provenance_and_merges(tmp_path):
    path = package(
        tmp_path,
        '<w:p><w:pPr><w:pStyle w:val="child"/></w:pPr><w:r><w:t>Title</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr>'
        "<w:p><w:r><w:t>A</w:t><w:tab/><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        {
            "word/styles.xml": f'<w:styles xmlns:w="{W}"><w:style w:styleId="child">'
            '<w:basedOn w:val="base"/></w:style><w:style w:styleId="base">'
            '<w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style></w:styles>'
        },
    )
    doc = ingest_docx(path)
    assert doc.document_id == sha256(path.read_bytes()).hexdigest()
    assert doc.blocks[0].heading_level == 2
    cell = doc.blocks[1].children[0].children[0]
    assert cell.column_span == 2 and cell.vertical_merge == "continue"
    assert cell.children[0].text == "A\tB"
    assert cell.source.endswith("tbl[1]/tr[0]/tc[0]")


def test_tracked_text(tmp_path):
    doc = ingest_docx(
        package(
            tmp_path,
            "<w:p><w:del><w:r><w:delText>old</w:delText></w:r></w:del><w:ins><w:r><w:t>new</w:t></w:r></w:ins></w:p>",
        )
    )
    assert doc.blocks[0].text == "new"
    assert doc.warnings


@pytest.mark.parametrize("kind", ["size", "expanded", "members", "entity", "invalid"])
def test_rejections(tmp_path, kind):
    path = package(tmp_path, "<w:p/>")
    limits = DocxLimits()
    expected = ErrorCode.LIMIT_EXCEEDED
    if kind == "size":
        limits = DocxLimits(max_file_bytes=1)
    elif kind == "expanded":
        limits = DocxLimits(max_uncompressed_bytes=1)
    elif kind == "members":
        path = package(tmp_path, "", {"other": "x"})
        limits = DocxLimits(max_members=1)
    else:
        expected = ErrorCode.INVALID_DOCX
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<!DOCTYPE x [<!ENTITY x "bad">]><x/>' if kind == "entity" else "<broken",
            )
    with pytest.raises(IngestionError) as caught:
        ingest_docx(path, limits=limits)
    assert caught.value.code == expected


def test_external_image_not_fetched(tmp_path):
    rels = (
        '<Relationships><Relationship Id="r1" Type="x/image" '
        'Target="https://invalid.example/image" TargetMode="External"/></Relationships>'
    )
    doc = ingest_docx(package(tmp_path, "", {"word/_rels/document.xml.rels": rels}))
    assert doc.images[0].external and doc.images[0].sha256 is None
    assert doc.warnings


def test_embedded_image_identity(tmp_path):
    payload = b"image-bytes"
    rels = (
        '<Relationships><Relationship Id="r1" Type="x/image" '
        'Target="media/image.png"/></Relationships>'
    )
    path = package(
        tmp_path,
        "",
        {
            "word/_rels/document.xml.rels": rels,
            "word/media/image.png": payload,
        },
    )
    image = ingest_docx(path).images[0]
    assert image.sha256 == sha256(payload).hexdigest()
    assert image.size_bytes == len(payload)


def test_utf16_entity_rejected(tmp_path):
    path = tmp_path / "unsafe.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "x">]><x/>'.encode("utf-16"))
    with pytest.raises(IngestionError) as caught:
        ingest_docx(path)
    assert caught.value.code == ErrorCode.INVALID_DOCX
