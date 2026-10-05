# AEGIS-X

Autonomous Mission Intelligence System: a local-first intelligence and research
system for sensitive technical knowledge.

## Development roadmap

Open [the interactive roadmap](docs/roadmap.html) in a browser for phases,
checkpoints, current status, and links to real-document inspection outputs.
It uses a local Inter font and works without a server or internet connection.
Status is a manually maintained snapshot; it does not read live Git/test results.
Minimal local demos will accompany phases where visual inspection helps validate
behavior. The production frontend remains a separate later phase.

## Current status

Phase 0 is complete. Phase 1 implements local PDF ingestion, provenance,
optional text geometry and geometric reading order. A local experiment workspace
preserves baseline, layout, and normalized text comparisons across saved runs.
Retrieval, reasoning, inference, and user interfaces are not implemented.
Air-gapped operation is a design goal, not a validated capability at this stage.

## Development setup

Requires Python 3.12 and uv. Initial setup downloads development dependencies;
do not use private documents during setup.

```bash
uv sync --locked
```

uv creates `.venv`, installs the `aegis-x` distribution in editable mode, and
installs the development tools pinned in `uv.lock`. Application code imports
the package as `aegis`. Ubuntu's system Python is unaffected.

## Verification

```bash
uv run --locked python --version
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

Expected: Python 3.12.x, passing packaging and PDF ingestion tests, successful lint and
format checks, and a wheel plus source archive in `dist/`.

## Repository layout

- `src/aegis/`: installable Python package; responsibilities are added as implemented.
- `tests/`: automated checks.
- `pyproject.toml`: package metadata, dependencies, build and tool configuration.
- `.python-version`: Python minor version selected by uv.
- `uv.lock`: resolved dependency versions; commit alongside dependency changes.

The `src/` layout requires package installation before import, helping reveal
packaging errors rather than accidentally importing code from the repository root.
pytest uses importlib mode without adding the source directory to its import path.

Local document data under `data/`, environment files, and generated artifacts
are ignored by Git. Git ignore rules are not an authorization or security boundary.

## PDF ingestion

Place a local PDF under the ignored `data/` directory and use the Python API:

```python
from aegis.ingestion import IngestionError, IngestionLimits, ingest_pdf

try:
    document = ingest_pdf(
        "data/report.pdf",
        limits=IngestionLimits(max_file_bytes=100 * 1024 * 1024, max_pages=2000),
    )
    print(document.document_id, len(document.pages))
    for page in document.pages:
        print(page.number, page.status, len(page.text))
except IngestionError as error:
    print(error.code, str(error))
```

Flow: validate the local file and size, read a bounded snapshot, hash those exact
bytes with SHA-256, open them with PyMuPDF, enforce encryption/page rules, then
return immutable records. There are no network calls or persistent writes in
the ingestion function. The application logger records the hash and page count,
not the source path or extracted text. PyMuPDF can emit its own diagnostics.

`document_id` identifies exact bytes, not semantic equivalence. Renaming a file
preserves its ID; modifying metadata or resaving may change it. The result records
the absolute source path, file size, PDF metadata, parser version, and whether
PyMuPDF repaired the PDF. Dates remain raw PDF metadata strings, not trusted timestamps.

Pages preserve one-based physical PDF numbering, text, and dimensions in points.
Each page is marked `text`, `image_only`, or `no_text`. Mixed documents retain
all pages; image-only detection is a heuristic and does not identify every scan.
If the entire PDF lacks extractable text, ingestion raises `no_extractable_text`.

Errors expose stable codes: `source_unreadable`, `invalid_pdf`, `encrypted_pdf`,
`limit_exceeded`, `no_extractable_text`, and `extraction_failed`.
Encrypted PDFs are rejected even when their user password is empty.
Unrecoverable corruption fails; PDFs repaired by the parser are explicitly marked.
Input must start with a PDF header; filename extensions do not determine format.

### Current limits

- Text uses PyMuPDF's sorted extraction; complex columns, tables, and equations
  do not have guaranteed reading order or preserved structure.
- Optional layout blocks and conservative normalization are available. No OCR,
  DOCX, chunking, production storage/CLI, or retrieval yet.
- Default limits are 100 MiB and 2,000 pages. File size and page limits do not
  bound decompression cost, extracted-text size, CPU time, or native parser memory.
  Parsing runs in-process; hostile-input process isolation is not implemented.
- Tests generate local fixtures; they do not establish quality on real research PDFs.

PyMuPDF is offered under AGPL or a commercial license; this repository has not
selected a distribution license. See the
[upstream licensing information](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright).

## Local experimental corpus

See [the test corpus guide](docs/test-corpus.md) for the curated multi-format
documents under `data/test-corpus/`, download provenance, inspection outputs,
and an example using a real technical handbook. No additional dependencies
are required by the corpus helpers.

Open [the experiment workspace](docs/experiments.html) to compare source pages,
baseline text, and ordered text blocks. See [experiment usage](docs/experiments.md)
to add local PDFs or inspect earlier saved runs.
