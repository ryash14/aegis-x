"""Measure real-corpus quality and growing mixed-format queues on this workstation."""

import argparse
import io
import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pymupdf

from aegis.chunking import ChunkConfig, chunk_document
from aegis.chunking.validation import validate_document
from aegis.ingestion import ingest_docx, ingest_pdf
from aegis.workspace.service import Workspace

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data/test-corpus"


def distance(left, right):
    previous = list(range(len(right) + 1))
    for index, a in enumerate(left, 1):
        row = [index]
        for offset, b in enumerate(right, 1):
            row.append(min(row[-1] + 1, previous[offset] + 1, previous[offset - 1] + (a != b)))
        previous = row
    return previous[-1]


def quality():
    cases = []
    expected = (
        "Docling bundles PDF document conversion to JSON and Markdown "
        "in an easy self contained package"
    )
    for name in ("ocr_test.pdf", "ocr_test_rotated_90.pdf", "ocr_test_rotated_180.pdf"):
        document = ingest_pdf(CORPUS / "docling/ocr" / name, include_ocr=True)
        actual = " ".join(document.pages[0].ocr.text.split())
        error = distance(actual, expected) / len(expected)
        cases.append(
            {
                "name": name,
                "check": "Manually transcribed short scan",
                "character_error_rate": round(error, 6),
                "passed": actual == expected,
            }
        )
    nasa = ingest_pdf(
        CORPUS / "reports/nasa-systems-engineering-handbook.pdf", include_normalized=True
    )
    page = nasa.pages[10]
    changes = [change.rule for block in page.normalized.blocks for change in block.changes]
    cases.append(
        {
            "name": "NASA page 11",
            "check": "Drop-cap and discretionary-hyphen repairs retain mappings",
            "passed": "This handbook" in page.normalized.text
            and "drop_cap" in changes
            and "soft_hyphen" in changes,
        }
    )
    chunks = chunk_document(nasa)
    coverage = validate_document(nasa, chunks, ChunkConfig())
    cases.append(
        {
            "name": "NASA full document",
            "check": "Complete chunk/source coverage",
            "passed": coverage["status"] == "complete",
            "chunks": len(chunks),
        }
    )
    word = ingest_docx(CORPUS / "docling/docx/word_tables.docx")

    def tables(blocks):
        return sum((block.kind == "table") + tables(block.children) for block in blocks)

    cases.append(
        {
            "name": "DOCX table fixture",
            "check": "Seven tables and outline heading retained",
            "passed": tables(word.blocks) == 7 and word.blocks[0].heading_level == 1,
        }
    )
    images = ingest_docx(CORPUS / "docling/docx/docx_grouped_images.docx")
    cases.append(
        {
            "name": "Grouped-image DOCX",
            "check": "Eight embedded-image relationships retain hashes",
            "passed": len(images.images) == 8 and all(image.sha256 for image in images.images),
        }
    )
    formula = ingest_docx(CORPUS / "docling/docx/equations.docx")
    cases.append(
        {
            "name": "Equation DOCX",
            "check": "Equation-layout limitation reported",
            "passed": any("Equations" in warning for warning in formula.warnings),
        }
    )
    return cases


def wait_jobs(workspace, ids):
    maximum = 0
    peak_controller = 0
    while True:
        with workspace.connection() as database:
            running = database.execute(
                "SELECT count(*) FROM documents WHERE status='processing'"
            ).fetchone()[0]
            remaining = database.execute(
                "SELECT count(*) FROM documents WHERE status IN ('queued','processing')"
            ).fetchone()[0]
        maximum = max(maximum, running)
        try:
            rss = (
                int(Path("/proc/self/statm").read_text().split()[1])
                * os.sysconf("SC_PAGE_SIZE")
                / 1024**2
            )
            peak_controller = max(peak_controller, rss)
        except OSError:
            pass
        if not remaining:
            break
        time.sleep(0.05)
    jobs = [workspace.get(identity) for identity in ids]
    return jobs, maximum, round(peak_controller, 2)


def synthetic_sources():
    with pymupdf.open() as document:
        for number in range(3):
            page = document.new_page()
            page.insert_text((40, 50), f"Engine report {number + 1}", fontsize=18)
            page.insert_textbox(
                pymupdf.Rect(40, 90, 540, 700),
                "Pressure remains stable at 42 kPa. Temperature is nominal. " * 16,
                fontsize=11,
            )
        pdf = document.tobytes()
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Engine checks</w:t></w:r></w:p>"
                "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Pressure</w:t></w:r></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>42 kPa</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
                "</w:body></w:document>"
            ),
        )
    return pdf, stream.getvalue(), (CORPUS / "docling/ocr/ocr_test_rotated_90.pdf").read_bytes()


def run_workload(root, count, sources):
    workspace = Workspace(root, workers=2)
    started = time.perf_counter()
    ids = []
    for index in range(count):
        kind = 2 if index % 16 == 0 else 1 if index % 4 == 0 else 0
        payload = sources[kind]
        name = f"document-{index}.{'docx' if kind == 1 else 'pdf'}"
        ids.append(workspace.upload(io.BytesIO(payload), len(payload), name)["id"])
    jobs, maximum, controller = wait_jobs(workspace, ids)
    elapsed = time.perf_counter() - started
    report = {
        "documents": count,
        "workers": 2,
        "succeeded": sum(job["status"] == "ready" for job in jobs),
        "failed": sum(job["status"] == "failed" for job in jobs),
        "elapsed_seconds": round(elapsed, 3),
        "documents_per_second": round(count / elapsed, 3),
        "max_active_jobs": maximum,
        "peak_worker_rss_mib": max(job["peak_rss_mib"] or 0 for job in jobs),
        "controller_rss_mib": controller,
        "chunks": sum(job["chunks"] for job in jobs),
    }
    workspace.close()
    recovered = Workspace(root, workers=2)
    report["restart_preserved_ready"] = (
        sum(recovered.get(identity)["status"] == "ready" for identity in ids) == count
    )
    recovered.close()
    return report


def run_corpus(root):
    workspace = Workspace(root, workers=2)
    paths = sorted(path for path in CORPUS.rglob("*") if path.suffix.lower() in {".pdf", ".docx"})
    started = time.perf_counter()
    ids = [workspace.add_file(path)["id"] for path in paths]
    jobs, maximum, controller = wait_jobs(workspace, ids)
    report = {
        "documents": len(jobs),
        "succeeded": sum(job["status"] == "ready" for job in jobs),
        "failed": [
            {"name": job["name"], "code": job["error_code"]}
            for job in jobs
            if job["status"] == "failed"
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "chunks": sum(job["chunks"] for job in jobs),
        "max_active_jobs": maximum,
        "peak_worker_rss_mib": max(job["peak_rss_mib"] or 0 for job in jobs),
        "controller_rss_mib": controller,
    }
    workspace.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[8, 32, 128])
    parser.add_argument("--report", type=Path, default=ROOT / "data/evaluation/phase1.json")
    args = parser.parse_args()
    root = (
        ROOT
        / "data/evaluation/runs"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    )
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "parser": pymupdf.VersionBind,
        "machine": platform.machine(),
        "quality": quality(),
        "workloads": [],
    }
    print(
        f"Quality: {sum(case['passed'] for case in report['quality'])}/{len(report['quality'])}",
        flush=True,
    )
    report["corpus"] = run_corpus(root / "corpus")
    print("Corpus:", json.dumps(report["corpus"]), flush=True)
    sources = synthetic_sources()
    for count in args.counts:
        result = run_workload(root / str(count), count, sources)
        report["workloads"].append(result)
        print("Workload:", json.dumps(result), flush=True)
    report["passed"] = (
        all(case["passed"] for case in report["quality"])
        and report["corpus"]["succeeded"] == 23
        and report["corpus"]["failed"] == [{"name": "2206.01062_pg3.pdf", "code": "encrypted_pdf"}]
        and all(
            run["failed"] == 0
            and run["max_active_jobs"] <= run["workers"]
            and run["restart_preserved_ready"]
            for run in report["workloads"]
        )
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    print("Report:", args.report, "passed:", report["passed"], flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
