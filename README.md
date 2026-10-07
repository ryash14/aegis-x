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

**Phase 1 complete:** PDF/DOCX extraction, local PDF OCR, normalization,
source-traceable chunking, persistent jobs, and a live inspection website.
Phase 2 is underway: persistent BM25 search and a live evidence-search demo are verified.
Dense embeddings, hybrid search and reranking remain. Production multi-user deployment remains
in later phases. See [the Phase 1 revision](docs/phase1-review.md).

## Open the application

```bash
uv sync --locked
uv run --locked aegis-workspace
```

Open **http://127.0.0.1:8765**. Upload multiple PDFs/DOCX files; inspect file sizes,
source previews, extraction methods, OCR confidence, and chunks with source
highlights. The library survives restarts. **Try sample documents** uses the downloaded
local corpus. See [workspace usage and checks](docs/workspace.md).

## Evidence search

Open http://127.0.0.1:8765/docs/retrieval.html and click **Refresh index**.
Search keywords across ready document chunks; inspect scores and source mappings,
then follow a result to its original document/page. See [sparse retrieval](docs/retrieval.md).

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

Expected: Python 3.12.x, passing ingestion, chunking, workspace and packaging tests, successful lint and
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
return immutable records. There are no network calls in the ingestion function;
opt-in OCR uses private temporary rasters that are deleted after recognition. The application logger records the hash and page count,
not the source path or extracted text. PyMuPDF can emit its own diagnostics.

`document_id` identifies exact bytes, not semantic equivalence. Renaming a file
preserves its ID; modifying metadata or resaving may change it. The result records
the absolute source path, file size, PDF metadata, parser version, and whether
PyMuPDF repaired the PDF. Dates remain raw PDF metadata strings, not trusted timestamps.

Pages preserve one-based physical PDF numbering, text, and dimensions in points.
Each page is marked `text`, `image_only`, or `no_text`. Mixed documents retain
all pages; image-only detection is a heuristic and does not identify every scan.
Without OCR, a PDF lacking native text raises `no_extractable_text`.
With `include_ocr=True`, recognized text can satisfy this requirement; native page
text and status remain unchanged, with OCR stored separately in `page.ocr`.

Errors expose stable codes: `source_unreadable`, `invalid_pdf`, `encrypted_pdf`,
`limit_exceeded`, `no_extractable_text`, and `extraction_failed`.
Encrypted PDFs are rejected even when their user password is empty.
Unrecoverable corruption fails; PDFs repaired by the parser are explicitly marked.
Input must start with a PDF header; filename extensions do not determine format.

### Current limits

- Text uses PyMuPDF's sorted extraction; complex columns, tables, and equations
  do not have guaranteed reading order or preserved structure.
- Optional layout blocks, conservative normalization, and resumable PDF batch
  ingestion are available. DOCX structure extraction is available separately. Optional local PDF OCR is available. Structure-aware chunking is available. No retrieval yet.
- Default limits are 100 MiB and 2,000 pages. File size and page limits do not
  bound decompression cost, extracted-text size, CPU time, or native parser memory.
  The Python API runs in-process. Batch jobs isolate parser/OCR subprocesses and
  enforce timeouts; this is not a hardened hostile-input sandbox.
- Generated-fixture tests and targeted real-corpus checks passed. General extraction
  accuracy remains unproven; see [measured results and boundaries](docs/phase1-review.md).

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

## Batch ingestion

```bash
uv run --locked python -m aegis.ingestion data/test-corpus --workers 2
```

Run again to reuse unchanged successful records. Known failures remain visible;
use `--retry-failed` to reattempt them. See [batch behavior](docs/batch-ingestion.md)
and [saved batch reports](docs/batches.html). The corpus includes five intentionally
unsupported scan/encryption cases, so this command returns exit code 1.

### DOCX inspection

Open [the DOCX workspace](docs/docx.html) to explore six local fixtures: tables,
images, equations, and headers/footers. Choose a document and expand source positions.
Rebuild a saved snapshot with `uv run python scripts/build_docx_demo.py`.
To inspect your own file: `uv run python scripts/build_docx_demo.py /path/to/document.docx`,
then refresh the workspace.

```python
from aegis.ingestion import ingest_docx

document = ingest_docx("data/test-corpus/docling/docx/word_tables.docx")
print(document.document_id, document.blocks[0])
```

DOCX extraction reads ZIP/XML locally without new dependencies. Paragraphs and
recursive tables retain body order, inherited outline levels, cell merge markers,
image relationship IDs, and zero-based XML child positions. Embedded images receive
SHA-256 identities; external references are never fetched. Input bytes, expanded
archive bytes, and member counts have configurable limits.

This is structural extraction, not Word rendering: no page numbers, image OCR,
list numbering reconstruction, equation layout, or header/footer extraction.
Equation tokens and inserted tracked text are retained; deleted text is excluded.
Merged cells retain markers rather than reconstructing a visual grid. PDF batch
jobs remain PDF-only; DOCX uses `ingest_docx` and the separate snapshot builder.

### Local PDF OCR

[Open OCR experiments](docs/experiments.html#ocr): choose the latest OCR run, then
compare Baseline with Local OCR on `ocr_test.pdf` and its rotated variants. Click a
recognized line to highlight its source region; expand details for low-confidence words.

```python
from aegis.ingestion import ingest_pdf

document = ingest_pdf("data/test-corpus/docling/ocr/ocr_test.pdf", include_ocr=True)
print(document.pages[0].ocr.text)
```

`--mode ocr` adds fallback OCR to resumable PDF batches. See [OCR behavior and
measurements](docs/ocr.md) for setup, limits, and testing your own scans.

### Structure-aware chunking

Open [chunk inspection](docs/chunks.html) to compare extracted source units with
bounded chunks, highlighted overlap, and native source references. The local run
includes large PDFs, DOCX tables, and rotated scans.

```python
from aegis.chunking import ChunkConfig, iter_chunks
from aegis.ingestion import ingest_pdf

document = ingest_pdf("data/report.pdf", include_normalized=True, include_ocr=True)
for chunk in iter_chunks(document, ChunkConfig(max_chars=1800, overlap_chars=160)):
    print(chunk.chunk_id, chunk.text, chunk.mappings)
```

For DOCX, pass the result of `ingest_docx` to the same chunking API. Add `--chunks`
to PDF batch ingestion to persist chunks alongside extraction records.
See [chunking behavior and limits](docs/chunking.md).
