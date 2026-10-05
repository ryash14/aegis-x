"""Bounded PDF jobs with immutable records, validated resume, and local run history."""

import fcntl
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pymupdf

from .errors import ErrorCode, IngestionError
from .job_store import JobStore
from .layout_models import LayoutConfig
from .models import IngestionLimits
from .normalization_models import NormalizationConfig
from .pdf import ingest_pdf


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identity(path: Path, limits: IngestionLimits) -> str:
    try:
        if not path.is_file():
            raise IngestionError(ErrorCode.SOURCE_UNREADABLE, "Source is not a readable file")
        if path.stat().st_size > limits.max_file_bytes:
            raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "PDF exceeds file size limit")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > limits.max_file_bytes:
                    raise IngestionError(ErrorCode.LIMIT_EXCEEDED, "PDF exceeds file size limit")
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise IngestionError(ErrorCode.SOURCE_UNREADABLE, "Could not read source file") from exc


def _atomic_json(path: Path, value: object) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class BatchConfig:
    mode: str = "text"
    workers: int = 1
    retry_failed: bool = False
    limits: IngestionLimits = IngestionLimits()
    timeout_seconds: float = 300

    def __post_init__(self) -> None:
        if self.mode not in {"text", "layout", "normalized"}:
            raise ValueError("Mode must be text, layout, or normalized")
        if self.workers < 1:
            raise ValueError("Workers must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")


def _pipeline_id(config: BatchConfig) -> str:
    implementation = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        implementation.update(source.name.encode())
        implementation.update(source.read_bytes())
    settings = {
        "record_schema": 1,
        "mode": config.mode,
        "limits": asdict(config.limits),
        "parser": pymupdf.VersionBind,
        "implementation": implementation.hexdigest(),
        "layout": asdict(LayoutConfig()),
        "normalization": asdict(NormalizationConfig()),
    }
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _worker(
    source: str, content_sha256: str, output: str, pipeline: str, config: BatchConfig
) -> dict:
    job = {"source_path": source, "pipeline_id": pipeline, "content_sha256": content_sha256}
    try:
        if _pipeline_id(config) != pipeline:
            return {
                **job,
                "status": "failed",
                "error_code": "pipeline_changed",
                "error": "Pipeline code changed during ingestion; rerun",
            }
        document = ingest_pdf(
            source,
            limits=config.limits,
            include_layout=config.mode == "layout",
            include_normalized=config.mode == "normalized",
        )
        if document.document_id != content_sha256:
            return {
                **job,
                "status": "failed",
                "error_code": "source_changed",
                "error": "Source changed during ingestion; rerun",
            }
        record = {"schema_version": 1, "pipeline_id": pipeline, "document": asdict(document)}
        if config.mode == "normalized":
            for saved, page in zip(record["document"]["pages"], document.pages, strict=True):
                saved["normalized"]["text"] = page.normalized.text
                saved["normalized"]["page_mappings"] = [
                    asdict(mapping) for mapping in page.normalized.mappings
                ]
        destination = Path(output)
        _atomic_json(destination, record)
        return {
            **job,
            "status": "succeeded",
            "output_path": str(destination),
            "output_sha256": _digest(destination),
            "pages": len(document.pages),
        }
    except IngestionError as exc:
        return {**job, "status": "failed", "error_code": exc.code, "error": str(exc)}
    except (OSError, RuntimeError, ValueError) as exc:
        return {**job, "status": "failed", "error_code": "worker_error", "error": str(exc)}


def _process(source: str, identity: str, output: str, pipeline: str, config: BatchConfig) -> dict:
    job = {"source_path": source, "pipeline_id": pipeline, "content_sha256": identity}
    request = {
        "source": source,
        "identity": identity,
        "output": output,
        "pipeline": pipeline,
        "config": asdict(config),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "aegis.ingestion.worker"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {**job, "status": "failed", "error_code": "timeout", "error": "PDF job timed out"}
    if result.returncode:
        return {
            **job,
            "status": "failed",
            "error_code": "worker_crashed",
            "error": f"PDF worker exited with code {result.returncode}",
        }
    prefix = "AEGIS_RESULT:"
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(prefix):
            response = json.loads(line[len(prefix) :])
            response["parser_diagnostics"] = result.stderr
            return response
    return {
        **job,
        "status": "failed",
        "error_code": "worker_crashed",
        "error": "Worker returned no result",
    }


def _discover(source: Path):
    if source.is_dir():
        yield from (
            path.absolute()
            for path in source.rglob("*")
            if path.suffix.lower() == ".pdf" and path.is_file()
        )
    else:
        yield source.absolute()


def _cached(job: dict | None, identity: str, records: Path) -> bool:
    if not job or job["content_sha256"] != identity or job["status"] != "succeeded":
        return False
    destination = Path(job["output_path"])
    try:
        return (
            destination.parent == records
            and destination.is_file()
            and _digest(destination) == job["output_sha256"]
        )
    except OSError:
        return False


def run_batch(source: str | Path, output: str | Path, config: BatchConfig | None = None) -> dict:
    """Process PDFs without a collection-size cap; jobs in flight are bounded by workers.

    Linux/Ubuntu coordinator lock prevents concurrent writers to the same output.
    Each worker still loads one entire PDF. File/page limits are not hard RAM bounds.
    """
    config = config if config is not None else BatchConfig()
    source = Path(source).expanduser().absolute()
    output = Path(output).expanduser().resolve()
    records = output / "records"
    reports = output / "reports"
    records.mkdir(parents=True, exist_ok=True)
    reports.mkdir(exist_ok=True)
    started = time.perf_counter()
    with (output / ".coordinator.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another ingestion run owns this output directory") from exc
        store = JobStore(output / "manifest.sqlite3")
        jobs = []
        pending = {}
        pipeline = _pipeline_id(config)

        def finish() -> None:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                job = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        **job,
                        "status": "failed",
                        "error_code": "worker_crashed",
                        "error": str(exc),
                    }
                store.save(result)
                jobs.append({**result, "outcome": result["status"]})

        try:
            with ThreadPoolExecutor(max_workers=config.workers) as pool:
                for path in _discover(source):
                    while len(pending) >= config.workers:
                        finish()
                    job = {"source_path": str(path), "pipeline_id": pipeline}
                    try:
                        identity = _identity(path, config.limits)
                    except IngestionError as exc:
                        result = {
                            **job,
                            "status": "failed",
                            "error_code": exc.code,
                            "error": str(exc),
                        }
                        store.save(result)
                        jobs.append({**result, "outcome": "failed"})
                        continue
                    previous = store.get(str(path), pipeline)
                    if _cached(previous, identity, records):
                        jobs.append({**previous, "outcome": "cached"})
                        continue
                    if (
                        previous
                        and previous["content_sha256"] == identity
                        and previous["status"] == "failed"
                        and not config.retry_failed
                    ):
                        jobs.append({**previous, "outcome": "failed_cached"})
                        continue
                    job.update(content_sha256=identity, status="processing")
                    store.save(job)
                    output_id = hashlib.sha256(
                        (str(path) + identity + pipeline).encode()
                    ).hexdigest()
                    future = pool.submit(
                        _process,
                        str(path),
                        identity,
                        str(records / (output_id + ".json")),
                        pipeline,
                        config,
                    )
                    pending[future] = job
                while pending:
                    finish()
        finally:
            store.close()
        if not jobs:
            raise ValueError("No PDF files found")
        identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
        report = {
            "id": identifier,
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "mode": config.mode,
            "workers": config.workers,
            "pipeline_id": pipeline,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "peak_worker_rss_mib": round(
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 2
            ),
            "total": len(jobs),
            "succeeded": sum(job["outcome"] == "succeeded" for job in jobs),
            "cached": sum(job["outcome"] == "cached" for job in jobs),
            "failed": sum(job["status"] == "failed" for job in jobs),
            "retried": config.retry_failed,
            "jobs": sorted(jobs, key=lambda job: job["source_path"]),
        }
        _atomic_json(reports / (identifier + ".json"), report)
        (reports / (identifier + ".js")).write_text(
            "window.AEGIS_BATCH_RUN = " + json.dumps(report).replace("<", "\\u003c") + ";\n"
        )
        catalog_path = reports / "catalog.json"
        catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else []
        catalog.append(
            {
                key: report[key]
                for key in ("id", "created_at", "total", "succeeded", "cached", "failed")
            }
        )
        _atomic_json(catalog_path, catalog)
        (reports / "catalog.js").write_text(
            "window.AEGIS_BATCH_RUNS = " + json.dumps(catalog) + ";\n"
        )
        return report
