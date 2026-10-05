"""Inspect local corpus PDFs in separate processes and save extraction previews."""

import argparse
import json
import resource
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect_pdf(path: Path, output: Path) -> dict:
    import pymupdf

    from aegis.ingestion import IngestionError, ingest_pdf

    started = time.perf_counter()
    record = {"source": str(path), "size_bytes": path.stat().st_size}
    try:
        with pymupdf.open(path) as pdf:
            record["pages"] = pdf.page_count
            if not pdf.needs_pass:
                record["pages_with_images"] = sum(bool(page.get_image_info()) for page in pdf)
        document = ingest_pdf(path)
        record.update(
            status="extracted",
            document_id=document.document_id,
            pages=len(document.pages),
            pages_without_text=sum(page.status != "text" for page in document.pages),
            characters=sum(len(page.text) for page in document.pages),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".json").write_text(json.dumps(asdict(document), indent=2))
        output.with_suffix(".txt").write_text(
            "\n".join(
                f"--- PAGE {page.number} [{page.status}] ---\n{page.text}"
                for page in document.pages
            )
        )
    except IngestionError as exc:
        record.update(status="rejected", error_code=exc.code, error=str(exc))
    except (RuntimeError, ValueError) as exc:
        record.update(status="unreadable", error=str(exc))
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    # Linux reports ru_maxrss in KiB. Each PDF runs in a fresh process.
    record["peak_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
    record["measurement_scope"] = "native PDF inspection plus ingestion and output serialization"
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "data" / "test-corpus")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(inspect_pdf(args.worker, args.output)))
        return 0
    corpus = args.corpus.resolve()
    manifest = json.loads((corpus / "manifest.json").read_text())
    output = corpus / "inspection"
    output.mkdir(exist_ok=True)
    records = []
    for entry in manifest["files"]:
        if entry["status"] != "downloaded" or Path(entry["path"]).suffix != ".pdf":
            continue
        path = corpus / entry["path"]
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(path),
                    "--output",
                    str(output / entry["path"]),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            record = json.loads(result.stdout.strip().splitlines()[-1])
            record["parser_diagnostics"] = result.stderr
        except (subprocess.SubprocessError, ValueError, IndexError) as exc:
            record = {"source": str(path), "status": "worker_failed", "error": str(exc)}
        records.append(record)
        print(f"{path.name}: {record['status']}, {record.get('pages', '?')} pages", flush=True)
    (output / "summary.json").write_text(json.dumps(records, indent=2) + "\n")
    print(f"Inspection saved: {output}")
    return int(any(record["status"] in {"worker_failed", "unreadable"} for record in records))


if __name__ == "__main__":
    raise SystemExit(main())
