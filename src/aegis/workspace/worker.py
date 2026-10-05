"""One isolated document job: parse, preserve methods, and persist paginated chunks."""

import json
import math
import resource
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf

from aegis.chunking import ChunkConfig, iter_chunks
from aegis.chunking.validation import validate_document
from aegis.ingestion import IngestionError, ingest_docx, ingest_pdf
from aegis.ingestion.batch import _atomic_json
from aegis.ingestion.docx import DocxLimits, ExtractedDocx
from aegis.ingestion.models import IngestionLimits
from aegis.ingestion.ocr_models import OCRConfig


def event(stage, done=0, total=0):
    print("AEGIS_EVENT:" + json.dumps({"stage": stage, "done": done, "total": total}), flush=True)


def extract(request):
    path = Path(request["source"])
    output = Path(request["output"])
    config = ChunkConfig(**request["chunks"])
    event("parsing")
    document = (
        ingest_docx(path, limits=DocxLimits(max_file_bytes=request["max_file_bytes"]))
        if request["format"] == "docx"
        else ingest_pdf(
            path,
            limits=IngestionLimits(max_file_bytes=request["max_file_bytes"]),
            include_normalized=True,
            include_ocr=True,
            ocr_config=OCRConfig(),
            progress_callback=lambda done, total, stage: event(stage, done, total),
        )
    )
    if document.document_id != request["identity"]:
        raise ValueError("Uploaded source changed during processing")
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "document_id": document.document_id,
        "format": request["format"],
        "pages": 0,
        "chunks": 0,
        "warnings": [],
        "methods": [],
        "page_chunk_counts": {},
    }
    if isinstance(document, ExtractedDocx):
        summary.update(
            blocks=len(document.blocks),
            warnings=list(document.warnings),
            methods=["structure", "chunks"],
        )
        _atomic_json(
            output / "structure.json",
            {
                "blocks": [asdict(block) for block in document.blocks],
                "images": [asdict(image) for image in document.images],
            },
        )
    else:
        summary.update(
            pages=len(document.pages),
            parser=document.parser_version,
            methods=["baseline", "layout", "normalized", "ocr", "chunks"],
        )
        with pymupdf.open(path) as pdf:
            for page in document.pages:
                saved = asdict(page)
                saved["layout_text"] = page.layout.text if page.layout else ""
                saved["normalized_text"] = page.normalized.text if page.normalized else ""
                if page.normalized:
                    saved["normalized"]["page_mappings"] = [
                        asdict(item) for item in page.normalized.mappings
                    ]
                geometry = pdf[page.number - 1]
                saved["display_width"] = geometry.rect.width
                saved["display_height"] = geometry.rect.height
                saved["rotation_matrix"] = list(geometry.rotation_matrix)
                saved["rotation"] = geometry.rotation
                _atomic_json(output / f"page-{page.number}.json", saved)
                if page.number % 25 == 0 or page.number == len(document.pages):
                    event("saving", page.number, len(document.pages))
    event("chunking")
    chunks = []
    index = []
    for chunk in iter_chunks(document, config):
        chunks.append(chunk)
        pages = sorted(
            {source.page for mapping in chunk.mappings for source in mapping.sources if source.page}
        )
        index.append(
            {
                "index": chunk.index,
                "pages": pages,
                "chars": len(chunk.text),
                "kind": chunk.kind,
                "title": chunk.headings[-1].text if chunk.headings else "",
            }
        )
        for page in pages:
            key = str(page)
            summary["page_chunk_counts"][key] = summary["page_chunk_counts"].get(key, 0) + 1
        _atomic_json(output / f"chunk-{chunk.index}.json", asdict(chunk))
        if len(chunks) % 25 == 0:
            event("chunking", len(chunks))
    coverage = validate_document(document, chunks, config)
    summary.update(
        chunks=len(chunks),
        coverage=coverage,
        chunk_config=asdict(config),
        peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
    )
    _atomic_json(output / "chunk-index.json", index)
    _atomic_json(output / "summary.json", summary)
    event("ready", len(chunks), len(chunks))


def preview(request):
    source = Path(request["source"])
    with pymupdf.open(source) as document:
        page = document[request["page"] - 1]
        scale = min(1.5, math.sqrt(2_000_000 / (page.rect.width * page.rect.height)))
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(request["output"])


def main():
    request = json.load(sys.stdin)
    try:
        if request.get("action") == "preview":
            preview(request)
        else:
            extract(request)
    except IngestionError as exc:
        print("AEGIS_ERROR:" + json.dumps({"code": exc.code, "message": str(exc)}), flush=True)
        raise SystemExit(1) from exc
    except (OSError, RuntimeError, ValueError, KeyError, IndexError) as exc:
        print(
            "AEGIS_ERROR:"
            + json.dumps({"code": "processing_failed", "message": "Document processing failed"}),
            flush=True,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
