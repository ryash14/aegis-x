"""Save local DOCX extraction snapshots for the offline inspection view."""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aegis.ingestion import ingest_docx

root = Path(__file__).resolve().parents[1]
output = root / "data/docx-experiments"
output.mkdir(parents=True, exist_ok=True)
run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("sources", nargs="*", type=Path, help="DOCX files; default: local corpus")
args = parser.parse_args()
sources = args.sources or sorted((root / "data/test-corpus/docling/docx").glob("*.docx"))
documents = []
for path in sources:
    documents.append({"name": path.name, "document": asdict(ingest_docx(path))})
if not documents:
    raise SystemExit("No local DOCX fixtures found")
run = {"id": run_id, "documents": documents}
(output / f"{run_id}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
(output / f"{run_id}.js").write_text("window.docxRun=" + json.dumps(run) + ";", encoding="utf-8")
catalog_path = output / "catalog.json"
catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
catalog.append(run_id)
catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
(output / "catalog.js").write_text(
    "window.docxCatalog=" + json.dumps(catalog) + ";", encoding="utf-8"
)
print(f"Saved {len(documents)} documents: {run_id}")
