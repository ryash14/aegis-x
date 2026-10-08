# AEGIS-X

A private, local-first technical research platform combining evidence-grounded
multi-document investigation with requirements revision review and contextual
consistency checks. Requirements intelligence is the flagship workflow.

The core private application, hybrid retrieval, cited research and revision review
are implemented. Requirements review enumerates explicit English requirements,
compares revisions, checks supported quantities and preserves human decision history.
Research uses a pinned local model with source/quotation validation and bounded
investigation. [Architecture](docs/architecture.md) describes the implemented scope
and limits; [deployment](docs/deployment.md) covers the single-host release.

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
follow-ups, **Search evidence** for retrieval filters, and **Revision review** for baseline/candidate
comparisons, source inspection, decisions and report export. Select a specific document
in Chat to prevent mixing revisions; a live timer includes queueing through the
validated response. Technical details are
collapsed by default. Broad summaries use bounded opening passages, not an
exhaustive review of every page.

A private Linux deployment can use the supplied systemd services and Caddy TLS
configuration. Public hosting requires a provisioned target host and domain.

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
`npm run test:research` checks the actual research and review workflow.
`npm run test:deployment` checks TLS login, restart and backup recovery with a verified
local Caddy binary; see the deployment guide.
Tests and evaluation helpers are retained because they validate application behavior.

## Resume demonstration

[Open the published resume demonstration](https://ryash14.github.io/aegis-showcase/).

`showcase/` is a standalone static walkthrough site. It includes a real synthetic-
document recording, explanation captions, architecture and honest validation limits.
The private application is separate from this public-facing demo.

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
