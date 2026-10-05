# Persistent PDF batch ingestion

```bash
uv run --locked python -m aegis.ingestion data/test-corpus --workers 2
```

The command accepts one local file or recursively discovers `.pdf` files in a
directory, including uppercase extensions. Other formats are ignored. The default
mode saves baseline text. Use `--mode layout` or `--mode normalized` to include
geometry and normalization; these modes cost more CPU, memory, and output space.
`--mode ocr` also includes normalization and fallback OCR for textless pages;
[OCR settings](ocr.md) and engine/model hashes become part of pipeline identity.

## Storage and resume

Default output is `data/ingestion/`:

- `manifest.sqlite3`: durable job status and input/output hashes.
- `records/`: versioned JSON extraction records, including document/page provenance.
- `reports/`: each batch run's results; earlier reports are preserved.

Unchanged successful documents are reused only when their saved JSON checksum
matches. Missing or modified outputs are rebuilt. Changed inputs, parser versions,
settings, or ingestion implementation code produce a different record identity.
Old records remain available. Identical files at different source paths retain
separate provenance records rather than sharing a misleading source path.

Parsing failures are remembered. Retry them explicitly:

```bash
uv run --locked python -m aegis.ingestion data/test-corpus --retry-failed --workers 2
```

Changing a failed input permits another attempt automatically. Interrupted jobs
remain recoverable on the next run. Output JSON is written to a private temporary
file, flushed, then renamed atomically. SQLite transactions protect manifest
updates. A Linux file lock prevents competing coordinators in the same output.

## Resource and failure handling

At most `--workers` jobs are in flight. Each PDF runs in its own subprocess;
timeouts and native parser crashes become failed jobs while other files continue.
A job timeout kills the worker process group, including OCR children; the
coordinator cleans that job’s temporary rasters.
`--timeout-seconds` defaults to 300. The source is rechecked against its scheduled
content hash before a record is saved. Pipeline code is also checked in the worker.

There is no limit on document count. Each worker still loads one whole PDF;
concurrency bounds the number of active files, not the RAM used by any one file.
Defaults remain 100 MiB and 2,000 pages per file. Override with `--max-file-mib`
and `--max-pages` after measuring your workload. More workers are not automatically faster.
Run reports keep per-document metadata in the coordinator; this is not a distributed
queue or an established million-document scalability claim.

Exit codes: `0` means every discovered document succeeded or was reused; `1` means
one or more new/remembered failures; `2` means invalid configuration or coordinator
failure. Without OCR, scan/encryption fixtures intentionally yield exit code `1`. With
OCR, the current scans succeed and encryption remains an explicit failure.

Open `docs/batches.html` for runs under the default output directory. The viewer
shows successful records, reused results, and explicit errors. Custom output paths
still produce reports but are not automatically added to that viewer.

Elapsed time includes source hashing, subprocess startup, parsing, record writing,
and manifest updates, ending before report serialization. Peak worker RSS is the
largest child-process peak on Linux during a fresh CLI invocation, not total RAM
across concurrent workers. Historical runs retain their original measurements.
