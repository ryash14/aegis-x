# AEGIS-X

A private, local-first technical research platform combining evidence-grounded
multi-document investigation with requirements revision review and contextual
consistency checks. Requirements intelligence is the flagship workflow.

Phases 1–3 are implemented: private accounts, owned projects, PDF/DOCX ingestion,
local OCR, source inspection, revision labels, persistent jobs and permanent deletion.
The [five-phase plan](docs/architecture.md) tracks the remaining research and review
features. Project-scoped keyword, semantic and hybrid search index ready documents automatically
and return bounded, source-linked evidence with document/revision/section filters.
Cited local-model answers and bounded investigations save inspectable evidence,
research traces, follow-ups and reports. Requirements intelligence remains Phase 4.

## Run the private app

Requires Python 3.12, uv and Tesseract for scanned PDFs. Create your account once;
the command prompts for a password of at least 12 characters.

```bash
uv sync --locked --extra dense
uv run --locked --extra dense python scripts/fetch_embedding_model.py
uv run --locked aegis-app user-add owner@aegis.local --name "Workspace owner"
uv run --locked aegis-app serve
```

Open http://127.0.0.1:8787, sign in, create a project and upload PDF/DOCX files.
Use **Documents** to upload and inspect sources, **Chat** for cited answers and
follow-ups, and **Search evidence** for retrieval filters. Select a specific document
in Chat to prevent mixing revisions; a live timer includes queueing through the
validated response. Technical details are
collapsed by default. Broad summaries use bounded opening passages, not an
exhaustive review of every page.

Private state persists under ignored `data/app/`. There are no default credentials.
See [operations](docs/workspace.md) for configuration and account administration.

The earlier single-user inspection/search tool remains available through
`uv run --locked --extra dense aegis-workspace` at http://127.0.0.1:8765,
using separate `data/workspace/` storage. It is a development tool; the new app
owns the authenticated product workflow.

For cited answers, start the local model in another terminal:

```bash
uv run --locked python scripts/start_local_model.py
```

## Architecture and completion plan

See [architecture](docs/architecture.md) for the problem, implemented components,
remaining product work, and deployment decisions.

## Verify

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

Browser regression checks: `npm ci` then `npm run test:private` for the private app
and `npm run test:browser` for the earlier workspace. With the local model running,
`npm run test:research` checks the actual research workflow.
Tests and evaluation helpers are retained because they validate application behavior.

## Reference

- [Workspace operations](docs/workspace.md)
- [Sparse search](docs/retrieval.md) and [dense search](docs/dense-retrieval.md)
- [Chunk provenance](docs/chunking.md), [OCR](docs/ocr.md), [batch CLI](docs/batch-ingestion.md)
- [Measured ingestion results](docs/phase1-review.md) and [test corpus](docs/test-corpus.md)

`src/aegis/` contains the application; `tests/` contains regression checks;
`scripts/` contains setup, corpus, and evaluation tools. Dependency locks and CI
support reproducible installation. Standalone experiment viewers and builders
were retired in favor of the live workspace.

PyMuPDF is offered under AGPL or a commercial license. This repository has not
selected a distribution license; resolve this before distributing a hosted product.
