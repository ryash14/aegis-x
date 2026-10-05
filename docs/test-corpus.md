# Local document test corpus

This fixed development corpus combines public technical documents with targeted
format/layout fixtures. It is intended for repeated local experiments, not as
a representative accuracy benchmark or a complete stress dataset.

## Storage and reproduction

Files live under `data/test-corpus/`, which Git ignores. The versioned selection
is `docs/test-corpus-sources.json`. The local `manifest.json` records source URLs,
resolved URLs, download timestamps, byte sizes, SHA-256 hashes, and failures.
Docling fixtures use an immutable Git revision and are checked against their
upstream Git blob hashes. Public report downloads retain their observed SHA-256;
the first download is not checked against a publisher-provided checksum.

```bash
uv run --locked python scripts/fetch_test_corpus.py
uv run --locked python scripts/inspect_test_corpus.py
```

Fetching requires internet access. Existing files whose hashes match the saved
manifest are reused. Partial downloads use temporary files, failed downloads
are retried, and one failure does not stop the others. Downloads use four threads
with a 200 MiB per-file ceiling. This is a corpus acquisition helper, not the
application's future batch-ingestion service.

Inspection requires no internet and reads only PDFs listed as downloaded in
the manifest. Each PDF runs in a fresh process with a 180-second timeout, saving
JSON extraction records and page-numbered text under `inspection/`. Rejections
and errors are saved in `inspection/summary.json`. Expected OCR/encryption
rejections do not make the inspection command fail.

## Selection

43 source documents across 14 formats:

| Format | Count | Purpose |
| --- | ---: | --- |
| PDF | 18 | Long technical reports, papers, columns, tables, equations, scans, rotations, encryption |
| DOCX | 6 | Text, tables, equations, grouped images, headers and footers |
| PPTX | 3 | Slides, images, charts |
| XLSX | 3 | Spreadsheets, charts, edge cases |
| CSV | 3 | Normal tables, quoted delimiters, inconsistent headers |
| HTML | 2 | Rich table cells and code |
| Markdown | 1 | Text and tables |
| JPG, PNG, WebP, TIFF | 1 each | Image/OCR experiments |
| ODT, ODS, DOC | 1 each | OpenDocument and legacy Word inputs |

Sources:

- [NASA Systems Engineering Handbook](https://www.nasa.gov/reference/systems-engineering-handbook/)
- [NIST AI RMF](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-ai-rmf-10)
- [Apollo Operations Handbook](https://ntrs.nasa.gov/api/citations/19710071423/downloads/19710071423.pdf)
- [Apollo 11 Flight Plan](https://www.archives.gov/files/historical-docs/doc-content/images/apollo-11-flight-plan.pdf)
- [Docling fixtures](https://github.com/docling-project/docling/tree/b38ed495f016e5cbd90cb888d5d4b35b89fdec01/tests/data)

Feature tags describe intended test coverage, not verified structural ground
truth. Selecting fixtures does not add Docling as an application dependency.
Preserve upstream document credits and licensing information. Public hosting
and inclusion in a fixture repository do not establish unrestricted rights to
redistribute every document. Downloaded documents are not committed or published.
There is no affiliation or endorsement by the source organizations.

## Practical inspection

Open these local files after running inspection:

- `reports/nasa-systems-engineering-handbook.pdf`: compare diagrams and tables
  in the original with `inspection/reports/nasa-systems-engineering-handbook.txt`.
- `reports/apollo-operations-handbook.pdf`: long-document workload; compare any
  physical PDF page with its `--- PAGE N ---` marker in the extracted text.
- `docling/pdf/code_and_formula.pdf`: inspect equation/code extraction limitations.
- `docling/ocr/ocr_test.pdf`: image-only document rejected pending OCR.
- `docling/docx/word_tables.docx`: stored for future DOCX support, not currently parsed.

To exercise the actual public API on a downloaded document:

```bash
uv run --locked python -c 'from aegis.ingestion import ingest_pdf; d = ingest_pdf("data/test-corpus/reports/nasa-systems-engineering-handbook.pdf"); print(d.document_id, len(d.pages)); print(d.pages[10].text)'
```

This prints the document identity, page count, and text from physical page 11.

## Interpreting measurements

`summary.json` reports per-file elapsed time and peak RSS in MiB on Linux.
Elapsed time covers native PDF inspection, ingestion, and JSON/text output;
process startup is excluded. Peak RSS includes the Python interpreter and native
parser. These are single-machine observations, not throughput or accuracy claims.
Extracted character counts establish that text exists, not that it is correct.

Only PDF extraction is currently supported. OCR failures, inaccurate reading
order, repeated text, and lost table/equation structure remain visible limitations.
Non-PDF files are retained for future phases. Hundreds or thousands of inputs
will require a separate workload test; this corpus establishes variety first.

## Initial observed run

All 43 files downloaded successfully: 56.50 MiB total, with 1,301 physical PDF
pages across 18 PDFs. All saved SHA-256 checksums and Office ZIP containers were
verified. A repeat acquisition reused every cached document.

13 PDFs returned text, four image-only/rotated fixtures reported
`no_extractable_text`, and one reported `encrypted_pdf`.

| Document | Pages | Inspection seconds | Peak RSS MiB |
| --- | ---: | ---: | ---: |
| NASA Systems Engineering Handbook | 297 | 8.526 | 116.96 |
| Apollo Operations Handbook | 917 | 3.881 | 122.85 |
| NIST AI RMF | 48 | 0.711 | 63.69 |

These results cover inspection and serialization as described above, on this
development machine. They do not demonstrate batch scalability or text fidelity.
The Apollo handbook has raster images on all 917 pages and also extractable text;
images alone therefore must not be treated as proof that OCR is necessary.
Inspecting physical page 11 of the NASA handbook revealed interleaved columns
and misplaced drop-cap text in the plain-text output. This is a concrete target
for the next structure-aware extraction checkpoint.
