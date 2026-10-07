# Phase 2 — local embeddings and dense retrieval

The dense retrieval checkpoint is complete. Hybrid fusion, reranking, and broader
reviewed relevance evaluation remain open.

## Try it

The configured workspace is running at http://127.0.0.1:8765/search.
Choose **Dense**, click **Refresh index**, and search:

> Are we building the right product for the customer?

In the current NASA handbook, dense search retrieves the inspected validation
section on physical page 21. Click the filename to open the source page; expand
source mappings to inspect the chunk and winning embedding window. Choose **BM25**
to compare the same query. Any/All term matching applies only to BM25.

## Setup and offline behavior

```bash
uv sync --locked --extra dense
uv run --locked --extra dense python scripts/fetch_embedding_model.py
uv run --locked --extra dense aegis-workspace
```

Only model acquisition requires network access. It uses curl to download public
artifacts at a fixed upstream revision, verifies versioned SHA-256 checksums,
and reuses already verified files. Models stay under the ignored
`data/models/bge-small-en-v1.5/`; document bytes never enter acquisition requests.
The manifest and checksums are also verified when loading the model.
Inference loads local files directly and never downloads missing assets.

The new optional dependencies are NumPy (vector operations), ONNX Runtime
(inference), and Hugging Face Tokenizers (the model's exact tokenizer). Transitive
versions are locked in `uv.lock`. PDF/DOCX/BM25 workflows remain available without
the dense extra. Model weights are not committed to Git.

## Why this model and index

We selected **BAAI/bge-small-en-v1.5**, an English 384-dimensional embedding model
with a 512-token input limit and MIT license, at revision
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`.
See the [upstream model card](https://huggingface.co/BAAI/bge-small-en-v1.5).
It is a compact baseline for the current English technical corpus; this checkpoint
does not establish that it is the best model for every future dataset.

The pinned ONNX model is approximately 127 MiB. CPU inference uses two intra-op
threads and one inter-op thread. This avoids a PyTorch/CUDA dependency and leaves
the GPU available for later local generation. We use the model's CLS pooling,
L2 normalization, and recommended query instruction; passages receive no query
instruction. See [ONNX Runtime](https://onnxruntime.ai/docs/api/python/api_summary.html)
and [Tokenizers](https://huggingface.co/docs/tokenizers/api/tokenizer).

We use an **exact cosine index** as the initial reference implementation. Normalized
vectors are persisted in SQLite as float32 blobs and scanned without loading the
whole collection into RAM. This avoids approximate-index tuning before establishing
retrieval behavior. Query work grows linearly with indexed vectors; this is not a
claim of constant-time search at unlimited scale. Later ANN decisions should use
measured latency and recall against this exact baseline.

## Technical flow

1. Refresh source chunks using the existing sparse snapshot index.
2. Tokenize each chunk without truncation. Up to 510 content tokens plus CLS/SEP
   fit the model. Longer chunks use 64-token overlap and retain token/character
   ranges for every window. All original chunk text and mappings stay intact.
3. Run local inference in batches of at most eight windows and normalize vectors.
4. Commit all windows for one chunk atomically. Read batches close before inference
   and writes; source identity is checked again before committing.
5. Embed the instructed query. Oversized queries are rejected rather than truncated.
6. Apply source/format/kind/page filters, compute cosine similarity, retain each
   chunk's best window, and return deterministic top-k source-linked chunks.

The model key covers pinned asset checksums, tokenizer/runtime/NumPy versions,
pooling, normalization, and window policy. Different keys never share vectors.
Unchanged chunks skip encoding. Source replacement/deletion cascades to vectors;
failed or removed jobs disappear immediately from live search. Interrupted indexing
can resume from committed chunks on the next refresh. Index counts update during
refresh, and concurrent dense refresh requests receive an explicit busy response.

`GET /api/search?method=dense&q=...` uses the same filters and result limit as BM25.
`POST /api/dense/index` requires the workspace mutation token; `GET /api/dense`
reports indexed windows, chunks, dimension, and model key.

## What was measured

- The saved local library encoded **970 chunks/windows in 77.042 seconds** on CPU.
  Reopening the workspace and refreshing reused those vectors.
- Eight human-authored, labeled English paraphrase cases were fixed before search:

| Metric | BM25 | Dense |
| --- | --- | --- |
| Recall@1 | 0.375 | 0.875 |
| Recall@3 | 0.375 | 1.000 |
| MRR | 0.400 | 0.9375 |

These are controlled development smoke cases, not a held-out benchmark or a claim
of corpus-wide improvement. Dense ranked validation second for one case, confusing
it with verification. BM25 also retrieves the real NASA page 21 query correctly;
the methods have different strengths.

Current sparse/dense/hybrid measurements are recorded in
[retrieval evaluation](retrieval-evaluation.json). Browser checks run against the
private app and write `data/evaluation/private-browser/report.json`.

```bash
uv run --locked --extra dense python scripts/evaluate_retrieval.py
npm run test:private
```
