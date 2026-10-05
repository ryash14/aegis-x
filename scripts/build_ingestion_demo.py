"""Save immutable local snapshots for baseline and layout extraction experiments."""

import argparse
import hashlib
import json
import resource
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pymupdf

from aegis.ingestion import (
    IngestionError,
    IngestionLimits,
    NormalizationConfig,
    OCRConfig,
    extract_ocr,
    ingest_pdf,
    normalize_layout,
)
from aegis.ingestion.layout import extract_layout
from aegis.ingestion.layout_models import LayoutConfig

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "test-corpus"
OUTPUT = ROOT / "data" / "experiments"
SAMPLES = {
    "reports/nasa-systems-engineering-handbook.pdf": (1, 11, 12),
    "reports/apollo-operations-handbook.pdf": (1, 500, 917),
    "reports/nist-ai-rmf.pdf": (1, 12),
    "docling/pdf/2203.01017v2.pdf": (1, 2),
    "docling/pdf/code_and_formula.pdf": (1, 2),
    "docling/pdf/table_mislabeled_as_picture.pdf": (1,),
    "docling/ocr/ocr_test.pdf": (1,),
    "docling/ocr/ocr_test_rotated_90.pdf": (1,),
    "docling/pdf_password/2206.01062_pg3.pdf": (1,),
}


def javascript(name: str, value: object) -> str:
    # Keep document text inert even if a saved artifact is embedded in HTML later.
    payload = json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")
    return f"window.{name} = {payload};\n"


def export_document(
    source: Path,
    pages: tuple[int, ...],
    directory: Path,
    key: str,
    ocr_config: OCRConfig | None = None,
) -> dict:
    with source.open("rb") as stream:
        snapshot = stream.read(IngestionLimits().max_file_bytes + 1)
    if len(snapshot) > IngestionLimits().max_file_bytes:
        raise ValueError("Source exceeds the ingestion file size limit")
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    identity = hashlib.sha256(snapshot).hexdigest()
    record = {"name": source.name, "source": str(source), "document_id": identity, "pages": []}
    started = time.perf_counter()
    baseline = None
    try:
        baseline = ingest_pdf(source)
        if baseline.document_id != identity:
            raise ValueError("Source changed during export; retry with a stable file")
        record["status"] = "extracted"
    except IngestionError as exc:
        record.update(status="rejected", error_code=exc.code, error=str(exc))
    with pymupdf.open(stream=snapshot, filetype="pdf") as pdf:
        record["page_count"] = pdf.page_count
        if pdf.needs_pass or record.get("error_code") == "encrypted_pdf":
            record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            return record
        for number in pages:
            if not 1 <= number <= pdf.page_count:
                raise ValueError(f"Page {number} is outside {source.name} ({pdf.page_count} pages)")
            page = pdf[number - 1]
            image_name = f"{key}-p{number}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(1.35, 1.35), alpha=False).save(
                directory / image_name
            )
            layout = extract_layout(page, LayoutConfig())
            normalized = normalize_layout(layout)
            normalized_by_id = {block.block_id: block for block in normalized.blocks}
            geometry = []
            by_id = {block.block_id: block for block in layout.blocks}
            for index, identifier in enumerate(layout.reading_order, 1):
                block = by_id[identifier]
                cleaned = normalized_by_id[identifier]
                rect = pymupdf.Rect(block.bbox) * page.rotation_matrix
                geometry.append(
                    {
                        "number": index,
                        "block_id": identifier,
                        "text": block.text,
                        "normalized_text": cleaned.text,
                        "normalization_changes": len(cleaned.changes),
                        "normalization_sources": sorted(
                            {
                                source.block_id
                                for mapping in cleaned.mappings
                                for source in mapping.sources
                            }
                        ),
                        "bbox": list(block.bbox),
                        "display_box": [
                            rect.x0 / page.rect.width * 100,
                            rect.y0 / page.rect.height * 100,
                            rect.width / page.rect.width * 100,
                            rect.height / page.rect.height * 100,
                        ],
                    }
                )
            ocr = None
            ocr_error = None
            ocr_blocks = []
            if ocr_config and not page.get_text("text").strip():
                try:
                    ocr = extract_ocr(page, ocr_config)
                    grouped = {}
                    for word in ocr.words:
                        grouped.setdefault((word.block, word.paragraph, word.line), []).append(word)
                    for index, words in enumerate(grouped.values(), 1):
                        bbox = pymupdf.Rect(words[0].bbox)
                        for word in words[1:]:
                            bbox |= pymupdf.Rect(word.bbox)
                        rect = bbox * page.rotation_matrix
                        ocr_blocks.append(
                            {
                                "number": index,
                                "block_id": f"ocr-{index}",
                                "text": " ".join(word.text for word in words),
                                "confidence": round(
                                    sum(word.confidence for word in words) / len(words), 1
                                ),
                                "low_confidence_words": sum(
                                    word.confidence < ocr_config.low_confidence_threshold
                                    for word in words
                                ),
                                "bbox": list(bbox),
                                "display_box": [
                                    rect.x0 / page.rect.width * 100,
                                    rect.y0 / page.rect.height * 100,
                                    rect.width / page.rect.width * 100,
                                    rect.height / page.rect.height * 100,
                                ],
                            }
                        )
                except IngestionError as exc:
                    ocr_error = {"code": exc.code, "message": str(exc)}
            record["pages"].append(
                {
                    "number": number,
                    "image": image_name,
                    "ocr": asdict(ocr) if ocr else None,
                    "ocr_error": ocr_error,
                    "ocr_blocks": ocr_blocks,
                    "ocr_included": ocr_config is not None,
                    "baseline_text": baseline.pages[number - 1].text
                    if baseline
                    else page.get_text("text", sort=True),
                    "layout_text": layout.text,
                    "blocks": geometry,
                    "layout": asdict(layout),
                    "normalized": asdict(normalized),
                    "normalized_text": normalized.text,
                    "rotation": page.rotation,
                    "warnings": list(layout.warnings),
                }
            )
    if any(page.get("ocr") and page["ocr"]["text"] for page in record["pages"]):
        record["status"] = "ocr_extracted" if not baseline else "extracted_with_ocr"
    record["parser_diagnostics"] = pymupdf.TOOLS.mupdf_warnings(reset=True)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Optional local PDF to add as a new saved run")
    parser.add_argument(
        "--pages", default="1", help="Comma-separated physical page numbers with --source"
    )
    parser.add_argument(
        "--ocr", action="store_true", help="Add offline OCR to sampled textless pages"
    )
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument("--ocr-language", default="eng")
    args = parser.parse_args()
    ocr_config = OCRConfig(dpi=args.ocr_dpi, language=args.ocr_language) if args.ocr else None
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    directory = OUTPUT / "runs" / identifier
    directory.mkdir(parents=True)
    sources = [(CORPUS / path, pages) for path, pages in SAMPLES.items()]
    if args.ocr:
        sources = [(path, (1,)) for path in sorted((CORPUS / "docling/ocr").glob("*.pdf"))]
        sources.extend(
            [
                (CORPUS / "reports/nasa-systems-engineering-handbook.pdf", (11,)),
                (CORPUS / "docling/pdf_password/2206.01062_pg3.pdf", (1,)),
            ]
        )
    if args.source:
        sources = [
            (
                args.source.expanduser().resolve(),
                tuple(dict.fromkeys(int(n) for n in args.pages.split(","))),
            )
        ]
    documents = []
    for index, (source, pages) in enumerate(sources):
        try:
            document = export_document(source, pages, directory, f"document-{index}", ocr_config)
        except (OSError, RuntimeError, ValueError) as exc:
            document = {
                "name": source.name,
                "source": str(source),
                "status": "failed",
                "error": str(exc),
                "pages": [],
            }
        documents.append(document)
        print(f"{source.name}: {document['status']}", flush=True)
    run = {
        "schema_version": 2,
        "id": identifier,
        "created_at": datetime.now(UTC).isoformat(),
        "parser_version": pymupdf.VersionBind,
        "layout_algorithm": "xy-cut-v1",
        "normalization_algorithm": "conservative-v1",
        "normalization_config": asdict(NormalizationConfig()),
        "layout_config": asdict(LayoutConfig()),
        "experiments": ["baseline", "layout", "normalized"] + (["ocr"] if args.ocr else []),
        "ocr_config": asdict(ocr_config) if ocr_config else None,
        "documents": documents,
        "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        "scope": "Sample pages; full-document baseline, sample-page geometry and rasterization",
    }
    (directory / "run.json").write_text(json.dumps(run, indent=2))
    (directory / "run.js").write_text(javascript("AEGIS_RUN", run))
    catalog_path = OUTPUT / "catalog.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
    catalog.append({"id": identifier, "created_at": run["created_at"], "documents": len(documents)})
    for filename, content in (
        ("catalog.json", json.dumps(catalog, indent=2)),
        ("catalog.js", javascript("AEGIS_EXPERIMENTS", catalog)),
    ):
        temporary = OUTPUT / (filename + ".part")
        temporary.write_text(content)
        temporary.replace(OUTPUT / filename)
    print(f"Saved run: {identifier}\nViewer: {ROOT / 'docs' / 'experiments.html'}")
    return int(any(document["status"] == "failed" for document in documents))


if __name__ == "__main__":
    raise SystemExit(main())
