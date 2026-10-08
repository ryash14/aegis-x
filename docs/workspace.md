# Private application operations

The authenticated app runs locally or on a private Linux host. Processing,
embeddings and generation use local runtimes. See [deployment](deployment.md) for
TLS, systemd, readiness and backup/restore.

```bash
uv sync --locked
uv run --locked aegis-app user-add owner@aegis.local --name "Workspace owner"
uv run --locked aegis-app serve
```

Open http://127.0.0.1:8787. Account provisioning prompts twice for a password
of at least 12 characters. There is no public registration or default password.
Use `aegis-app user-disable EMAIL` to disable an account and revoke its sessions.
Use `aegis-app user-password EMAIL` to change a password and revoke existing sessions.
Accounts named admin have the same project ownership restrictions as other users;
account administration is performed through the local CLI.
Provision additional accounts with `user-add`; each sees only their own projects.

Create a project, optionally describe it, and upload PDFs/DOCX files. Upload settings
include chunk size/overlap, revision and reference/baseline/candidate role. Revision
labels can be edited afterward. Inspect native text, layout, normalization, local
OCR, tables, source coordinates and traceable chunks. Extraction failures expose
an explicit retry. Unsupported/encrypted files fail without interrupting other jobs.

Document deletion permanently removes the original, extraction artifacts, job record
and indexed evidence; it terminates an active parser. Project deletion removes all
of its documents. Interrupted deletions finish at startup. Completed work and valid
sessions persist across restart; interrupted processing returns to the queue.

## Search project evidence

Install the dense extra and verified embedding assets once:

```bash
uv sync --locked --extra dense
uv run --locked --extra dense python scripts/fetch_embedding_model.py
```

Restart the app afterward. Search has its own **Search evidence** view. Ready
files index automatically; newly uploaded files may still be embedding. Choose
Hybrid, Keyword or Semantic search, filter by document/revision/role/section/kind/
format and open **Inspect exact source**. Surrounding evidence is limited to adjacent
chunks with the same heading context and evidence kind. Context defaults to 12,000
characters and never exceeds the requested 1,000–24,000 character budget. Scores
are ranking signals, not confidence probabilities. Cited research uses the separate workflow described below.

The authenticated APIs are `GET /api/search` and `GET /api/search/status` with the
selected project in `X-Aegis-Project`. Query parameters additionally support PDF
page, 1–20 results and context budget. Missing model assets leave keyword search
available; semantic/hybrid requests return a clear unavailable response.

## Cited research and investigations

Start the verified local model in a second terminal, then start the app:

```bash
uv run --locked python scripts/start_local_model.py
uv run --locked --extra dense aegis-app serve
```

The model stays private at `127.0.0.1:11435`; it is not a browser-facing endpoint.
Use **Chat** for evidence-grounded research. Choose a cited answer or multi-step
investigation. Ready documents must finish indexing first. Every accepted claim
has a source citation: click it to view the saved excerpt, highlighted exact quote,
source hash, ranges and warnings, then open the original chunk. A model support
review is labelled separately from deterministic quotation validation.

Saved research can be reopened from history (including older pages). Enter another
question and use **Ask follow-up** to link it to the selected completed run while
retrieving fresh evidence. **Cancel run** terminates active requests. **Retry** creates
a new run from a failed/cancelled run. Completed results offer a JSON evidence report.
Partial/insufficient-evidence answers display unresolved questions rather than a
confident fabricated summary. Semantic support checks remain fallible; inspect sources.

Runs share one model runner and have explicit call/time/context limits. The default
wall-clock limit is 180 seconds. Long cold starts can fail within that budget; warm
up the runtime or configure an appropriate limit before starting the app. On restart,
interrupted research is marked failed and requires retry; completed research persists.
Source deletion also erases saved research derived from it and its follow-ups.

Authenticated research routes: `POST /api/research`, `GET /api/research`,
`GET /api/research/ID`, `POST /api/research/ID/cancel`, `POST /api/research/ID/retry`,
`GET /api/research/ID/evidence/EVIDENCE_ID` and `GET /api/research/ID/report`.
All mutations require the session CSRF token; all records are project-owner scoped.

## Configuration

Environment variables are read when the process starts; `.env` files are not loaded
automatically. `--storage PATH` before the subcommand overrides the storage location.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AEGIS_STORAGE` | `data/app` | Private database, session key and document artifacts |
| `AEGIS_MODEL_URL` | `http://127.0.0.1:11435` | Loopback-only pinned generation runtime |
| `AEGIS_RESEARCH_TIMEOUT` | `180` | Research wall-clock budget, 10–600 seconds |
| `AEGIS_EMBEDDING_MODEL` | `data/models/bge-small-en-v1.5` | Verified local BGE assets |
| `AEGIS_WORKERS` | `2` | Shared parser workers across all projects |
| `AEGIS_MAX_FILE_MIB` | `100` | Maximum individual upload size |
| `AEGIS_JOB_TIMEOUT` | `300` | Parser time limit in seconds |
| `AEGIS_SESSION_SECONDS` | `28800` | Absolute session lifetime in seconds |
| `AEGIS_ALLOWED_HOSTS` | `127.0.0.1,localhost,::1` | Explicit accepted hostnames |
| `AEGIS_COOKIE_SECURE` | `0` | Set `1` when served through HTTPS |
| `AEGIS_PUBLIC_ORIGIN` | empty | Exact browser origin behind a TLS proxy |

At most four uploads are streamed simultaneously; additional requests receive 429.
Two parser subprocesses run by default, with lazy previews and paginated document/
chunk lists. The PDF limit is 2,000 pages. These bounds limit concurrent work, but
are not a hard RAM or total disk quota. Run one app process for each storage directory;
multiple replicas and public hosting remain release work.

Passwords use Argon2. Session tokens are stored as hashes, with HttpOnly/SameSite
cookies, CSRF checks, expiry and login throttling. Every artifact route checks
ownership. Secure cookies and an explicit trusted HTTPS origin are required for
external serving. The CLI defaults to loopback. Ollama is not exposed by this app.

Keep the entire storage directory, including `session.key`, private and persistent.
Database schema version 2 is created transactionally; newer unsupported versions
are refused. Backup/restore tooling and deployment verification belong to later phases.

## Verify the private app

```bash
uv run --locked pytest -q
npm run test:private
npm run test:research # requires the local model server
```

The browser check uses temporary accounts and storage, with local public fixtures
from the existing test corpus. It writes screenshots and a report under ignored
`data/evaluation/private-browser/`. Tests cover two-user HTTP isolation, PDF/DOCX/OCR,
permanent deletion, active worker cancellation, persistence and restart recovery.

---

The following section documents the earlier development inspection server, which
uses separate storage and different removal semantics.

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
scan after [fetching the corpus](test-corpus.md). The live workspace replaces the retired standalone experiment viewers.

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


## Revision review

Open **Revision review**, choose two ready documents as baseline and candidate, and
start a comparison. English shall/must/should statements are enumerated from up to
2,000 extracted chunks per document, capped at 1,000 requirements. Stable IDs match
first; unlabelled similar wording is only a suggested match. Duplicate IDs are
ambiguous. Source quotations retain offsets, chunk links and document hashes.

Supported single-bound quantities are converted and compared only with unchanged
subject, modality and conditions. Different conditions, unsupported units, compound
expressions and implicit/table-only requirements need manual review. Inventory
coverage describes extracted material scanned, not exhaustive specification review.

Inspect baseline and candidate quotations side by side, record accepted/rejected/
needs-followup with a reason, and export JSON or printable HTML. Decisions append to
a hash-linked history; this is not an externally anchored tamper-proof audit.
**Investigate this change** opens a cited research question tied to the review ID,
finding ID and result hash. Review findings remain human proposals.

Completed identical comparisons reuse cached results against source hashes, parser
signatures, revision metadata and comparison version. Cancel/retry and saved review
history are available. Source deletion removes derived reviews and their decisions.
Processing uploads can be cancelled and retried after the prior worker stops.

Authenticated routes: `GET/POST /api/reviews`, `GET /api/reviews/ID`,
`POST /api/reviews/ID/cancel`, `/retry`, `/decisions`, and
`GET /api/reviews/ID/report?format=json|html`. Project ownership and CSRF rules
apply to reviews and decisions exactly as they do to document/research mutations.
