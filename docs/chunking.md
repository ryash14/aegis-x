# Structure-aware chunking

Chunks are bounded text records for later indexing. This step builds them from
extraction records; embeddings, search, and retrieval are not implemented yet.
No new dependency or network service is used.

## Explore

Open `docs/chunks.html`. The left panel highlights selected extraction-unit text;
the right panel shows the assembled chunk. Repeated overlap is highlighted in the
chunk. Expand provenance to inspect native PDF lines, OCR words, or DOCX paragraph
text and their original offsets. PDF experiments remain available for page images.
The source panel contains extracted units, not a rendered original document.

Try the NASA handbook for normalized blocks, `word_tables.docx` for row boundaries,
and rotated scans for OCR coordinates. Prev/next and chunk selection retain the
saved output. Documents load individually; earlier snapshots remain available.

Build another run from your own local documents:

```bash
uv run --locked python scripts/build_chunk_demo.py /path/to/report.pdf /path/to/report.docx
```

With no paths, the helper processes all local corpus PDFs and DOCX files. It saves
per-document JSON/JS, verifies textual-unit coverage and source offsets, and appends
run history under ignored `data/chunk-experiments/`. Private text stays local.

## Contract and boundaries

`iter_units(document, config)` yields ordered extracted units.
`iter_chunks(document, config)` streams chunks without concatenating an entire
document's text. `chunk_document` materializes the same output for inspection or
persistence. Extraction itself still holds a full document in memory.

Defaults: 1,800 Unicode characters per chunk and up to 160 characters of overlap.
These are character bounds, not model-token limits. Token sizing will depend on the
embedding/LLM models selected in later phases. No collection-size cap is introduced.

- PDF: normalized blocks are preferred, then native layout blocks, then baseline
  page text. Chunks never cross physical pages or extraction-method boundaries.
  Short blocks mostly containing larger fonts can establish a heading; this is
  explicitly marked as a heuristic, with level 1 and unverified hierarchy.
- OCR: paragraphs follow the engine's block/paragraph IDs. Native text takes
  precedence when present. Words keep OCR-text offsets, confidence, and source
  boxes; OCR heading inference is not attempted.
- DOCX: outline levels establish section hierarchy. Chunks do not cross heading
  changes or mix a table with surrounding prose. Table rows remain intact when
  they fit the character budget. Cell text uses generated tabs and paragraph
  separators; nested tables are flattened with a warning. Merge markers remain
  in the original extracted document; a visual merged grid is not reconstructed.

Compatible small units pack together. An oversized unit splits preferentially at
sentence/newline boundaries, then whitespace, then a hard character boundary for
long unbroken strings. Every character of each nonempty textual unit remains
covered. Empty/image-only DOCX paragraphs and wholly blank PDF pages produce no
text chunk. Normalization's intentional deletions remain in extraction change logs.

Overlap applies only between pieces of an oversized unit, not across natural page,
section, paragraph-packing, or table boundaries. Actual overlap may be less than the
configured amount to ensure progress. It can begin inside a word and is recorded
explicitly as `overlap_prefix_chars`; it is context, not additional source content.

## Source mapping

Each chunk records parent document hash, source path, ordinal, section headings,
fragment ranges, warnings, and output-to-source mappings. All offsets are half-open
Python Unicode character indices. Inserted separators have empty sources and an
explicit `separator` kind.

Source domains identify native PDF line text, baseline page text, OCR text, or a
DOCX paragraph XML path. Normalized copy ranges clip exactly into native lines.
Transformed characters, such as expanded ligatures, retain the original transformed
source range even if a chunk boundary cuts through the expansion. PDF geometry is
line-level and OCR geometry is word-level, not exact character geometry.

Chunk IDs are deterministic hashes of document identity, algorithm, config,
ordinal, text, fragments, mappings, and headings. Source path is recorded separately;
identical content copied to another path keeps chunk IDs while retaining its own
source path. Changed text, positions, or chunk settings change the IDs.

## Validation on real documents

Saved run `20261005T165947Z-b21308`: 24 inputs, 23 verified, encrypted PDF rejected.
Across 23 extracted documents, 5,008 chunks cover 1,539,054 structural-unit characters.
For every chunk, the helper checks bounded size, contiguous output mappings, source
range validity, exact copied text, and complete unit coverage accounting for overlap.

| Document | Chunks | Largest chunk |
| --- | ---: | ---: |
| Apollo operations handbook, 917 pages | 3,981 | 1,800 characters |
| NASA systems engineering handbook, 297 pages | 751 | 1,800 characters |
| DOCX table fixture | 14 | 188 characters |

Coverage does not prove semantic chunk quality, reading-order accuracy, or correct
section inference. PDF tables and formulas remain extracted text, headers/footers
remain, and heuristic headings can misclassify labels. Reviewing chunk usefulness
and measuring growing workloads is the next Phase 1 checkpoint.
