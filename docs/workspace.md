# Live document workspace

Start from the repository root:

```bash
uv sync --locked
uv run --locked aegis-workspace
```

Open http://127.0.0.1:8765. The workspace runs locally; its font, scripts,
previews, parsing, and OCR do not need a remote service. Setup dependencies and
corpus downloads require network access. PDF scans require Tesseract with English
and orientation data; see [OCR setup](ocr.md).

## Inspect documents

1. Drop several PDFs/DOCX files or click **Upload documents**. Each card shows
   its size and job status. Upload and parsing progress are separate.
2. Select a ready file. PDFs have page navigation, source boxes, an original PDF
   viewer, and **Baseline / Layout / Normalized / OCR / Chunks** tabs. DOCX has
   a structural preview and **Structure / Chunks** tabs.
3. Hover a block or select a chunk to highlight its source. Expand source details
   to inspect exact ranges, XML positions, OCR confidence, or normalization edits.
4. Change chunk size and overlap in settings before uploading new documents.
   Limits count Unicode characters. Existing outputs keep their original settings.
5. Search and page through the library. Failed files remain visible with an
   explicit retry. Removal hides a job; it does not erase its stored files.

**Try sample documents** adds the local NASA handbook, a DOCX table fixture, and a rotated
scan after [fetching the corpus](test-corpus.md). Earlier experiments remain linked
from the sidebar, including saved baseline/layout/normalization/OCR runs.

## Persistence and bounds

Sources, SQLite job records, and per-page/per-chunk outputs live in the ignored
`data/workspace/`. Stop with Ctrl+C; restart with the same storage directory.
Completed jobs stay ready, and interrupted jobs return to the queue. One running
workspace owns a storage directory. Extraction outputs record their code/parser/OCR
signature; restarting does not silently rewrite older results.

```bash
uv run --locked aegis-workspace --workers 2 --max-file-mib 100 --timeout-seconds 300
```

Defaults: two document workers, 100 MiB per upload, 2,000 PDF pages, 300 seconds
per document. Previews are lazy and bounded separately. The library and chunk lists
are paginated, with no fixed document-count ceiling. Jobs use isolated subprocesses;
timeouts terminate their process groups. A worker still holds one document in
memory, so these limits are not a hard RAM guarantee. Disk capacity and extraction
cost determine practical scale.

The server binds to loopback and checks Host, Origin, and a session mutation token.
This is a single-user inspection workspace. Production identity, deployment, and
multi-user access belong to later phases.

## Reproduce checks

Python checks do not require downloaded documents:

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

For real-document browser checks, fetch the corpus, install the development-only
Node dependency, and use an installed Chrome/Chromium browser:

```bash
npm ci
npm run test:browser
uv run --locked python scripts/evaluate_phase1.py
```

Set `AEGIS_BROWSER` to a browser executable if it is not `/usr/bin/google-chrome`.
The browser check creates an isolated temporary workspace; it does not modify
uploaded documents in your normal library. Reports and screenshots go under
`data/evaluation/`. Versioned measurement snapshots are
[phase1-evaluation.json](phase1-evaluation.json) and
[phase1-browser-check.json](phase1-browser-check.json).
