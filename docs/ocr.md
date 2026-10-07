# Local PDF OCR

Tesseract 5.5.0 runs locally on CPU; English (`eng`) and orientation (`osd`) data
are installed on this workstation. No new Python dependency or GPU model is needed.
On another Ubuntu machine:

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd
```

## Try it

Upload a scan in the live workspace at http://127.0.0.1:8765.
Select the document and open **OCR** to inspect recognized text, confidence,
and source boxes. The standalone snapshot viewer and builder have been retired.

For resumable whole-document ingestion:

```bash
uv run --locked python -m aegis.ingestion data/test-corpus --mode ocr --workers 2
```

## Extraction contract

`ingest_pdf(source, include_ocr=True, ocr_config=OCRConfig(...))` applies OCR only
to pages without native text. This includes image-only pages and textless vector
pages. Native `page.text`, status, layout, and normalization stay separate.
Mixed pages containing native text skip OCR; page warnings flag image regions
that might contain unread text. This does not perform region-level OCR or repair
poor existing text layers.

`page.ocr` records recognized text, word-level half-open Unicode output offsets,
engine block/paragraph/line/word IDs, confidence, and bounding boxes in unrotated
PDF points. Parent document SHA-256 and one-based physical page number complete
provenance. Spaces and line separators are reconstructed from engine hierarchy;
OCR words refer to the raster, not native PDF text spans. Tables and equations
are not reconstructed into structured objects.

The engine receives a private page raster, first estimates orientation, then
recognizes using automatic page segmentation (`--psm 3`). Right-angle corrections
are recorded and reversed when mapping boxes to source coordinates. Sparse pages
can lack an orientation estimate; the rotation stays unchanged and a warning is
recorded. Low-confidence estimates are also flagged for review. Source files are
never modified. Recognition models and executable are hashed for record/cache identity.
See the official [Tesseract CLI documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
and [PyMuPDF page geometry documentation](https://pymupdf.readthedocs.io/en/latest/page.html).

## Bounds and errors

Defaults: English, 200 DPI, 20 million raster pixels, 60 seconds per page OCR,
and one Tesseract thread per invocation. Batch concurrency remains `--workers`;
whole-document job timeout defaults to 300 seconds. CLI settings include
`--ocr-language`, `--ocr-dpi`, `--ocr-max-pixels`, `--ocr-timeout-seconds`, and
`--no-ocr-orientation`. Install language packs before selecting another language.

Pixel limits are checked before rasterization. They are not a hard process RAM
cap. A page OCR timeout covers orientation and recognition after engine setup;
batch job timeouts also cover parsing/setup and kill descendants. Temporary page
rasters are cleaned after normal/error completion and batch job timeout. An abrupt
crash outside the batch coordinator can still leave OS temporary files.

Missing engine/data produces `ocr_unavailable`; recognition failure produces
`ocr_failed`; page timeout produces `ocr_timeout`; excessive raster size produces
`limit_exceeded`. Blank or unreadable pages retain warnings; a document with neither
native nor recognized text still fails with `no_extractable_text`. Whole-document
OCR errors fail that batch job and remain available for explicit retries.

Confidence is an engine score, not an accuracy probability. Words below 70 are
flagged and retained. Handwriting, arbitrary skew, multilingual layouts, image
regions on native-text pages, structured tables, and complex formulas remain
unvalidated. No general OCR accuracy or unlimited scalability claim is made.

## Measured local results

Saved experiment `20261005T164108Z-b48586`, 200 DPI:

| Fixture | Words | Rotation correction | Recognition time |
| --- | ---: | ---: | ---: |
| Upright scan | 15 | 0° | 1.1 s |
| 180° scan | 15 | 180° | 1.1 s |
| 90° scan | 15 | 90° | 1.1 s |
| Rotation-mismatch scan | 189 | 0° | 2.0 s |

The three short scans exactly match the same manually reviewed 15-word source
phrase after whitespace normalization. The denser scan has three words below
threshold; its full transcription accuracy has not been scored.

Full corpus batch: 18 PDFs, 17 succeeded, encrypted PDF rejected, 25.911 seconds
with two workers; largest worker peak RSS 368.44 MiB. The unchanged rerun reused
17 records in 0.470 seconds. Peak RSS is the largest completed worker peak, not
summed concurrent memory. These measurements apply to this corpus and workstation.
