# Phase 1 revision — document intelligence

Phase 1 is complete for PDF and DOCX ingestion, local PDF OCR, traceable chunking,
persistent batches, and the live inspection workspace. This review records the Phase 1 handoff. The [sparse retrieval baseline](retrieval.md)
is now the first completed Phase 2 checkpoint.

## What we built and why

| Step | Behavior | Purpose |
| --- | --- | --- |
| Upload | Stream bounded source bytes into a private job directory; calculate SHA-256 | Stable identity and reproducible input |
| Native PDF extraction | Preserve physical pages, text, metadata, and parser version | Keep the baseline available for comparison |
| Layout | Preserve spans and coordinates; estimate column reading order | Inspect where text came from and how order changes |
| Normalization | Conservatively repair whitespace, soft hyphens, and drop caps; retain edits and source ranges | Clean text without losing its origin |
| Scan fallback | Apply local Tesseract only to textless pages; retain orientation, confidence, and boxes separately | Make scans usable while keeping native extraction intact |
| DOCX structure | Read bounded ZIP/XML; preserve body order, headings, tables, and image references | Retain document structure without inventing Word pages |
| Chunking | Pack compatible structural units; split oversized units; preserve complete source coverage | Produce bounded evidence units for retrieval |
| Persist and inspect | Queue isolated workers; store per-page and per-chunk outputs; preview lazily | Recover from restarts and explore real documents |

A document hash identifies exact bytes. Chunk IDs also account for configuration
and mappings. Re-uploading under another filename preserves content identity;
resaving the document can change it. Jobs have their own IDs because the same
source can be processed with different settings.

The default chunk budget is **1,800 Unicode characters with 160-character overlap**,
not a model token budget. Chunks respect PDF pages, extraction methods, DOCX
sections, and table-row units. Overlap is used when splitting oversized units;
it is not injected between every paragraph. Source mappings use half-open ranges
and retain the original page/block/line or DOCX XML position.

Before a job becomes ready, validation checks chunk sizes, source ranges, copy
fidelity, fragment fidelity, and complete text-unit coverage. Coverage proves that
chunking retained extracted text; it does not prove the parser read the original
page correctly. The separate quality checks address selected extraction cases.

## Try the actual application

Open http://127.0.0.1:8765 and click **Try sample documents**, or upload your own files.

- **NASA handbook, page 11:** compare Baseline, Layout, and Normalized. Inspect
  the repaired opening “This handbook” and its source ranges, then view Chunks.
- **Rotated scan:** Baseline has no native text; OCR shows recognized lines,
  confidence, and source boxes. Chunks use that OCR output.
- **Word tables:** inspect the structural preview, select table chunks, and expand
  XML source positions. DOCX has no fabricated physical page numbers.
- Upload an encrypted PDF alongside valid documents: its failure stays isolated.
  Refresh or restart the server to inspect the same completed jobs again.

The source PDF viewer, file sizes, multiple uploads, method tabs, chunk settings,
retry, removal, and mobile layout were exercised in Chrome. No JavaScript errors
or external HTTP requests were observed during that check.

## Measured results

Measurements were taken on this workstation with Python 3.12.15, PyMuPDF 1.28.2,
and two CPU document workers. The GPU was not required.

| Workload | Result | Time |
| --- | --- | --- |
| Targeted quality cases | 8/8 passed | Included in evaluation |
| Full supported corpus | 23 ready; 1 expected encrypted rejection; 5,008 chunks | 38.617 s |
| Mixed queue: 8 documents | 8 ready; no failures | 1.857 s |
| Mixed queue: 32 documents | 32 ready; no failures | 6.356 s |
| Mixed queue: 128 documents | 128 ready; no failures | 25.045 s |

Mixed queues use small native PDFs, DOCX tables, and one scan per 16 documents.
All three preserved completed results after reopening their storage; active jobs
never exceeded two. They are repeatable workload measurements, not an accuracy
benchmark of 128 unrelated large PDFs. The full corpus includes the 297-page NASA
and 917-page Apollo handbooks.

The three short upright/rotated scans matched a manual transcription exactly
(character error rate 0). Other checks cover NASA normalization and chunk coverage,
DOCX tables, image hashes, and explicit equation limitations. This small suite does
not establish general OCR or reading-order accuracy.

The full-corpus largest worker peak RSS was 273.93 MiB; controller RSS was 197.83
MiB. Worker RSS is a process high-water measure and may include inherited launch
memory; it is not simultaneous total system consumption. There is no hard memory
sandbox. See [raw results](phase1-evaluation.json) and
[browser checks](phase1-browser-check.json).

## Boundaries to remember

PDF table grids, equations, complex reading order, handwriting, and arbitrary
languages are not guaranteed. DOCX preserves table structures and image references;
it does not render Word layout, OCR its images, reconstruct list numbering, or
extract headers/footers. Unsupported formats in the broader corpus are fixtures
for later work, not supported uploads.

There is no fixed 10/100-document cap. Bounded workers, pagination, disk storage,
and lazy previews prevent the interface from loading the whole library at once.
One worker still parses a complete bounded document. More workers increase memory
pressure; more documents consume disk and queue time. The local workspace is an
inspection tool, not the later production multi-user service.

We now have source-traceable evidence units. Phase 2 begins with sparse
retrieval over those chunks, followed by local embeddings, hybrid ranking,
reranking, and reviewed retrieval metrics. No retrieval or model reasoning is
implemented in Phase 1.
