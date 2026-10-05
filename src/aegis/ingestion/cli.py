"""Local PDF batch-ingestion command."""

import argparse
from pathlib import Path

from .batch import BatchConfig, run_batch
from .models import IngestionLimits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a local PDF or PDF directory with validated resume"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/ingestion"))
    parser.add_argument("--mode", choices=("text", "layout", "normalized"), default="text")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-file-mib", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=2000)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    try:
        config = BatchConfig(
            args.mode,
            args.workers,
            args.retry_failed,
            IngestionLimits(args.max_file_mib * 1024 * 1024, args.max_pages),
            args.timeout_seconds,
        )
        report = run_batch(args.source, args.output, config)
    except (OSError, RuntimeError, ValueError) as exc:
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
