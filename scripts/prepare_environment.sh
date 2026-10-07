#!/usr/bin/env bash
# One entry point for dependencies and the selected local model assets.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p data/setup
exec > >(tee data/setup/setup.log) 2>&1
uv sync --locked --extra dense
command -v tesseract >/dev/null
tesseract --list-langs
if [[ ! -d node_modules/playwright ]]; then
    npm ci
fi
PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000 npx playwright install ffmpeg
uv run --locked --extra dense python scripts/fetch_embedding_model.py
uv run --locked --extra dense python scripts/prepare_local_runtime.py
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
