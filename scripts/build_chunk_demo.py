"""Save traceable chunk experiments and verify source coverage on local documents."""

import argparse
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aegis.chunking import ChunkConfig, chunk_document, iter_units
from aegis.ingestion import IngestionError, ingest_docx, ingest_pdf
from aegis.ingestion.docx import ExtractedDocx

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/chunk-experiments"


def validate(document, units, chunks, config):
    """Check coverage and output/source ranges against actual immutable extraction."""
    by_id = {unit.unit_id: unit for unit in units}
    coverage = {key: [] for key, unit in by_id.items() if unit.text.strip()}
    source_text = {}
    if isinstance(document, ExtractedDocx):

        def index_paragraphs(blocks):
            for block in blocks:
                if block.kind == "paragraph":
                    source_text[block.source] = block.text
                index_paragraphs(block.children)

        index_paragraphs(document.blocks)
    else:
        for page in document.pages:
            source_text[f"native_page_text:{page.number}"] = page.text
            if page.ocr:
                source_text[f"ocr_text:{page.number}"] = page.ocr.text
            if page.layout:
                for block in page.layout.blocks:
                    for index, line in enumerate(block.lines):
                        source_text[f"native_line:{page.number}:{block.block_id}:{index}"] = (
                            line.text
                        )
    for chunk in chunks:
        if not 0 < len(chunk.text) <= config.max_chars:
            raise ValueError("Chunk length limit violated")
        cursor = 0
        for mapping in chunk.mappings:
            if mapping.output_start != cursor or not cursor < mapping.output_end <= len(chunk.text):
                raise ValueError("Output mappings have a gap or invalid range")
            cursor = mapping.output_end
            if not mapping.sources and mapping.kind != "separator":
                raise ValueError("Text has no source")
            for source in mapping.sources:
                key = source.xml_path or (
                    f"native_line:{source.page}:{source.block_id}:{source.line_index}"
                    if source.domain == "native_line"
                    else f"{source.domain}:{source.page}"
                )
                value = source_text[key]
                if not 0 <= source.start < source.end <= len(value):
                    raise ValueError("Source range outside original extraction")
                if (
                    mapping.kind == "copy"
                    and value[source.start : source.end]
                    != chunk.text[mapping.output_start : mapping.output_end]
                ):
                    raise ValueError("Copy mapping differs from original extraction")
        if cursor != len(chunk.text):
            raise ValueError("Incomplete chunk mapping")
        for fragment in chunk.fragments:
            unit = by_id[fragment.unit_id]
            if (
                unit.text[fragment.unit_start : fragment.unit_end]
                != chunk.text[fragment.output_start : fragment.output_end]
            ):
                raise ValueError("Chunk fragment differs from structural unit")
            coverage[unit.unit_id].append((fragment.unit_start, fragment.unit_end))
    for key, ranges in coverage.items():
        cursor = 0
        for start, end in sorted(ranges):
            if start > cursor:
                raise ValueError("Source unit has missing text")
            cursor = max(cursor, end)
        if cursor != len(by_id[key].text):
            raise ValueError("Source unit is not completely covered")
    return source_text


def javascript(name, value):
    return f"window.{name}=" + json.dumps(value).replace("<", "\\u003c") + ";\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", type=Path)
    parser.add_argument("--max-chars", type=int, default=1800)
    parser.add_argument("--overlap-chars", type=int, default=160)
    args = parser.parse_args()
    config = ChunkConfig(max_chars=args.max_chars, overlap_chars=args.overlap_chars)
    corpus = ROOT / "data/test-corpus"
    sources = args.sources or sorted(
        path for path in corpus.rglob("*") if path.suffix.lower() in {".pdf", ".docx"}
    )
    if not sources:
        raise SystemExit("No local PDF or DOCX sources found")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    directory = OUTPUT / "runs" / run_id
    directory.mkdir(parents=True)
    documents = []
    for index, path in enumerate(sources):
        started = time.perf_counter()
        entry = {
            "name": path.name,
            "source": str(path.absolute()),
            "file": f"document-{index}",
            "chunks": 0,
            "status": "failed",
        }
        try:
            document = (
                ingest_docx(path)
                if path.suffix.lower() == ".docx"
                else ingest_pdf(path, include_normalized=True, include_ocr=True)
            )
            units = tuple(iter_units(document, config))
            chunks = chunk_document(document, config)
            source_text = validate(document, units, chunks, config)
            entry.update(
                status="verified",
                document_id=document.document_id,
                chunks=len(chunks),
                units=len(units),
                source_characters=sum(len(unit.text) for unit in units),
                coverage="complete",
                max_chunk_chars=max((len(chunk.text) for chunk in chunks), default=0),
            )
            record = {
                "document_id": document.document_id,
                "source_path": document.source_path,
                "config": asdict(config),
                "chunks": [asdict(chunk) for chunk in chunks],
                "units": [
                    {"unit_id": unit.unit_id, "text": unit.text, "kind": unit.kind}
                    for unit in units
                ],
                "source_text": source_text,
            }
            with (directory / f"document-{index}.json").open("w") as stream:
                json.dump(record, stream)
            with (directory / f"document-{index}.js").open("w") as stream:
                stream.write(javascript("AEGIS_CHUNK_DOCUMENT", record))
            del document, units, chunks, source_text, record
        except IngestionError as error:
            entry.update(error_code=error.code, error=str(error))
        entry["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        documents.append(entry)
        print(f"{path.name}: {entry['status']} · {entry['chunks']} chunks", flush=True)
    run = {
        "id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "config": asdict(config),
        "algorithm": "structure-v1",
        "documents": documents,
    }
    (directory / "run.json").write_text(json.dumps(run, indent=2))
    (directory / "run.js").write_text(javascript("AEGIS_CHUNK_RUN", run))
    catalog_path = OUTPUT / "catalog.json"
    catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
    catalog.append({"id": run_id, "created_at": run["created_at"], "documents": len(documents)})
    catalog_path.write_text(json.dumps(catalog, indent=2))
    (OUTPUT / "catalog.js").write_text(javascript("AEGIS_CHUNK_CATALOG", catalog))
    print(f"Saved run: {run_id}\nViewer: {ROOT / 'docs/chunks.html'}")
    return int(any(item["status"] == "failed" for item in documents))


if __name__ == "__main__":
    raise SystemExit(main())
