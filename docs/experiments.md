# Ingestion experiments

Open `docs/experiments.html` directly in a browser. The workspace is separate
from the application and uses the existing project environment; no server or
additional dependencies are required.

## Compare experiments

- **Baseline text:** original sorted PyMuPDF text, preserved unchanged.
- **Layout & reading order:** native text blocks, lines, spans, fonts, and bounding
  boxes; a geometric order determines how blocks are displayed.
- **Saved run:** earlier snapshots remain available. Changing runs does not rebuild
  or overwrite previous extraction results.
- **Document / sample page:** choose an exported physical PDF page. Click a source
  box or text block to highlight the corresponding region.

For the NASA handbook, compare physical page 11 in both experiments. Layout
ordering keeps the left column together before the right column. The drop-cap
`T` remains a separate native line; normalization is still pending.
Scanned PDFs show source images alongside the explicit missing-text error.
Encrypted documents show the rejection without attempting password recovery.

## Build another snapshot

```bash
uv run --locked python scripts/build_ingestion_demo.py
uv run --locked python scripts/build_ingestion_demo.py --source data/test-corpus/reports/nasa-systems-engineering-handbook.pdf --pages 11,12
```

Each build creates a new `data/experiments/runs/<id>/` directory with rendered
sample pages, structured records, and baseline/layout text. The catalog indexes
all saved runs. These local artifacts may contain document text and are ignored
by Git. Do not publish them when testing private inputs.

The default build samples nine corpus PDFs, including the 917-page Apollo
handbook, equation/table fixtures, scans, and encryption. It performs full-document
baseline extraction and renders only selected sample pages. This helper is
not the production resumable batch-ingestion implementation.

## Core API

```python
from aegis.ingestion import ingest_pdf

document = ingest_pdf("data/report.pdf", include_layout=True)
page = document.pages[0]
baseline = page.text
layout = page.layout
```

Layout is opt-in so existing callers retain baseline behavior and cost. Layout
records preserve native blocks/lines/spans rather than guessing semantic sections.
The parent document hash, page number, and native block ID provide source identity.
Coordinates are unrotated PDF points; the exporter transforms overlay coordinates
with the page rotation matrix to match rendered previews.

## Reading-order method and limits

The `xy-cut-v1` heuristic splits groups at empty vertical gutters, then horizontal
gaps. Within unsplittable groups it sorts top-to-bottom, then left-to-right.
Spanning text blocks can force a horizontal split before column processing.
An explicit stack avoids Python recursion limits. Gap thresholds are configurable
with `LayoutConfig` and use PDF points.

This is intended for simple horizontal left-to-right columns. Nonhorizontal text
retains native block order with a warning. Right-to-left text, tables, equations,
overlapping figures, captions, and drop caps need further handling. Font metadata
and positions are preserved; semantic headings and table cells are not inferred.
The fixture and NASA example checks do not establish corpus-wide reading-order accuracy.

Tests cover spanning headings/footers, column order, unchanged baseline output,
span/coordinate preservation, rotations, empty/overlapping blocks, and many-block
inputs. The browser workspace checks switching experiments/runs/documents/pages,
overlays, error displays, and responsive rendering.
