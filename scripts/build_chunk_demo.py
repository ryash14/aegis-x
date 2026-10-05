"""Save traceable chunk experiments and verify source coverage on local documents."""

import argparse
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aegis.chunking import ChunkConfig, chunk_document, iter_units
from aegis.chunking.validation import source_texts, validate_document
from aegis.ingestion import IngestionError, ingest_docx, ingest_pdf

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/chunk-experiments"


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
            validate_document(document, chunks, config)
            source_text = source_texts(document)
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
