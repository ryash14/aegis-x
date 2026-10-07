# Phase 2 — sparse retrieval baseline

The first Phase 2 checkpoint is complete. Dense retrieval, hybrid fusion,
reranking, and reviewed retrieval-quality evaluation are still pending.

## Use it

Start `uv run --locked aegis-workspace`, then open
http://127.0.0.1:8765/docs/retrieval.html or **Evidence search** in the document
sidebar. Upload documents first; wait for their jobs to become ready.

1. Click **Refresh index**. It indexes ready extraction snapshots and reuses
   unchanged ones. Refresh again after new uploads finish.
2. Search `systems engineering` with **All terms**, or `requirements verification`
   with **Any term**. Select PDF/DOCX to restrict the results.
3. Inspect the ranked chunk text, score and source mappings. Click a filename to
   open its exact source document/page in the extraction workspace.

Removed and failed jobs are excluded from live searches immediately, even before
refreshing the index. Refresh prunes their indexed records. Index counts describe
stored records; they can include inactive jobs until the next refresh.

## Implementation and reason

We use Python's SQLite interface and **FTS5** rather than adding another search
service or Python dependency. SQLite builds must include FTS5 and JSON support.
This workstation and the GitHub verification runner support both.

- Chunk text enters an inverted index: search terms identify matching chunks.
- SQLite's built-in BM25 ranks lexical matches by term frequency, document length,
  and corpus statistics. Lower scores rank higher; scores are not probabilities.
- Unicode61 tokenization provides case-insensitive word matching and accent
  normalization. There is no stemming, stopword removal, or synonym expansion.
- Queries are converted into quoted literal terms joined by OR (**Any**) or AND
  (**All**). User text cannot supply FTS operators or SQL. Limits are 2,000 query
  characters, 64 distinct terms, and 100 results per call.
- Ranking ties use chunk ID, job ID, then chunk index for deterministic order.
- SQL filters support job ID, PDF/DOCX format, chunk kind, and physical PDF page.
  Filtering restricts candidates; BM25 statistics still use the whole index.
- Each result retains the original chunk ID, document hash, text, headings,
  fragments, mappings, and page/XML provenance. Search does not rewrite evidence.

See the [SQLite FTS5 reference](https://www.sqlite.org/fts5.html), particularly
Unicode61 and `bm25()`, for the underlying implementation.

The index resides in the existing ignored `data/workspace/workspace.sqlite3`, in
separate retrieval tables. A source replacement is transactional: invalid chunks
or interrupted writes cannot expose a partially replaced source. Refresh is atomic
per document, not across the entire library. New searches may see already committed
sources while later sources are indexing. Ready jobs keep their own source versions;
unchanged job/signature pairs skip reindexing. Multiple uploads are separate jobs,
so duplicate uploads can produce duplicate evidence and affect corpus statistics.

API examples:

```text
POST /api/retrieval/index             (requires workspace session token)
GET  /api/retrieval
GET  /api/search?q=pressure&mode=all&format=pdf&page=1&limit=10
```

`job_id` and `kind` are additional optional filters. Modes default to `any`.
The index and search remain local, with the existing loopback request checks.

## Evidence and limits

Automated checks cover ranking, persistence, idempotent refresh, transactional
rollback, source mappings, literal queries, accents, empty queries, parameter
bounds, metadata/page filters, and removed-job visibility. The real Chrome check
uploads NASA, DOCX tables, a rotated scan, and an encrypted PDF; indexes ready jobs;
searches NASA; follows a ranked result to its physical page; and then continues
all prior extraction checks. Additional live checks covered zero results and
mobile search layout, without JavaScript errors or remote HTTP requests.

This verifies the retrieval mechanism, not its general relevance quality.
A broad `systems engineering` search ranked NASA page 289 first in the current
library; lexical matching can prioritize index entries or repeated headers/footers.
BM25 can miss evidence using synonyms or paraphrases. Query judgments and
Recall@K/MRR/NDCG benchmarks are still a later Phase 2 checkpoint.

Next: select and evaluate a local embedding model against this sparse baseline,
then build dense retrieval before hybrid fusion and reranking. Phase 1's PDF table,
equation, OCR and reading-order limitations still affect the evidence being searched.
