# AEGIS-X

Autonomous Mission Intelligence System: a local-first intelligence and research
system for sensitive technical knowledge.

## Current status

Phase 0: environment and repository foundation. Document ingestion, retrieval,
reasoning, inference, and user interfaces are planned; they are not implemented.
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

Expected: Python 3.12.x, one passing packaging smoke test, successful lint and
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
