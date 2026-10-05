"""Local PDF batch-ingestion command."""

import argparse
from pathlib import Path

from aegis.chunking import ChunkConfig

from .batch import BatchConfig, run_batch
from .errors import IngestionError
from .models import IngestionLimits
from .ocr_models import OCRConfig


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a local PDF or PDF directory with validated resume"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/ingestion"))
    parser.add_argument("--mode", choices=("text", "layout", "normalized", "ocr"), default="text")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-file-mib", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=2000)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument("--ocr-max-pixels", type=int, default=20_000_000)
    parser.add_argument("--ocr-timeout-seconds", type=float, default=60)
    parser.add_argument("--no-ocr-orientation", action="store_true")
    parser.add_argument("--chunks", action="store_true", help="Persist traceable document chunks")
    parser.add_argument("--chunk-max-chars", type=int, default=1800)
    parser.add_argument("--chunk-overlap-chars", type=int, default=160)
    args = parser.parse_args()
    try:
        config = BatchConfig(
            args.mode,
            args.workers,
            args.retry_failed,
            IngestionLimits(args.max_file_mib * 1024 * 1024, args.max_pages),
            args.timeout_seconds,
            OCRConfig(
                language=args.ocr_language,
                dpi=args.ocr_dpi,
                max_pixels=args.ocr_max_pixels,
                timeout_seconds=args.ocr_timeout_seconds,
                detect_orientation=not args.no_ocr_orientation,
            ),
            ChunkConfig(max_chars=args.chunk_max_chars, overlap_chars=args.chunk_overlap_chars)
            if args.chunks
            else None,
        )
        report = run_batch(args.source, args.output, config)
    except (IngestionError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(2, f"Ingestion failed: {exc}\n")
    print(
        f"{report['total']} PDFs · {report['succeeded']} new · "
        f"{report['cached']} reused · {report['failed']} failed"
    )
    print(
        f"Elapsed: {report['elapsed_seconds']} s · "
        f"Report: {args.output / 'reports' / (report['id'] + '.json')}"
    )
    return int(report["failed"] > 0)
