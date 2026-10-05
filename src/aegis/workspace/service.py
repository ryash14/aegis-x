"""Durable local uploads, bounded subprocess jobs, restart recovery, and lazy previews."""

import fcntl
import hashlib
import json
import math
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pymupdf

from aegis.chunking import ChunkConfig
from aegis.ingestion import IngestionError
from aegis.ingestion.ocr import engine_identity
from aegis.ingestion.ocr_models import OCRConfig


class Workspace:
    def __init__(self, root, *, workers=2, max_file_bytes=100 * 1024 * 1024, timeout=300):
        if workers < 1 or max_file_bytes < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Workspace limits must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise RuntimeError("Another workspace owns this storage") from exc
        self.max_file_bytes = max_file_bytes
        self.timeout = timeout
        self.workers = workers
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.preview_slots = threading.BoundedSemaphore(2)
        self.db = self.root / "workspace.sqlite3"
        with self.connection() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, identity TEXT NOT NULL, config TEXT NOT NULL,
                    signature TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                    pages INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, error TEXT, created_at TEXT NOT NULL,
                    elapsed_seconds REAL, peak_rss_mib REAL
                );
                CREATE INDEX IF NOT EXISTS document_status ON documents(status, created_at);
            """)
            database.execute(
                "UPDATE documents SET status='queued',stage='queued',done=0,total=0 "
                "WHERE status='processing'"
            )
        implementation = hashlib.sha256()
        for directory in ("workspace", "ingestion", "chunking"):
            for path in sorted((Path(__file__).parent.parent / directory).glob("*.py")):
                implementation.update(path.name.encode())
                implementation.update(path.read_bytes())
        try:
            self.engine = asdict(engine_identity(OCRConfig()))
        except IngestionError as exc:
            self.engine = {"error": exc.code}
        self.signature = hashlib.sha256(
            json.dumps(
                {
                    "code": implementation.hexdigest(),
                    "parser": pymupdf.VersionBind,
                    "ocr": self.engine,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aegis-document")
        self.coordinator = threading.Thread(target=self._schedule, daemon=True)
        self.coordinator.start()

    @contextmanager
    def connection(self):
        database = sqlite3.connect(self.db, timeout=30)
        database.row_factory = sqlite3.Row
        try:
            with database:
                yield database
        finally:
            database.close()

    def update(self, identity, **values):
        allowed = {
            "status",
            "stage",
            "done",
            "total",
            "pages",
            "chunks",
            "error_code",
            "error",
            "elapsed_seconds",
            "peak_rss_mib",
            "signature",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("Invalid job update")
        with self.connection() as database:
            database.execute(
                "UPDATE documents SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                [*values.values(), identity],
            )

    def get(self, identity):
        with self.connection() as database:
            row = database.execute("SELECT * FROM documents WHERE id=?", (identity,)).fetchone()
        if not row or row["status"] == "removed":
            raise KeyError("Document not found")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def list(self, *, offset=0, limit=50, query=""):
        with self.connection() as database:
            where = "status!='removed' AND instr(lower(name),lower(?))>0"
            rows = database.execute(
                f"SELECT * FROM documents WHERE {where} "
                "ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (query, limit, offset),
            ).fetchall()
            count = database.execute(
                f"SELECT count(*) FROM documents WHERE {where}", (query,)
            ).fetchone()[0]
            states = dict(
                database.execute(
                    "SELECT status,count(*) FROM documents WHERE status!='removed' GROUP BY status"
                )
            )
        documents = [dict(row) for row in rows]
        for item in documents:
            item["config"] = json.loads(item["config"])
        return {
            "documents": documents,
            "total": count,
            "states": states,
            "offset": offset,
            "limit": limit,
        }

    def upload(self, stream, size, name, config=None):
        if not 0 < size <= self.max_file_bytes:
            raise ValueError("File exceeds upload limit or is empty")
        name = name.replace("\\", "/").split("/")[-1]
        if not name or len(name) > 240 or any(ord(character) < 32 for character in name):
            raise ValueError("Invalid filename")
        extension = Path(name).suffix.lower()
        if extension not in {".pdf", ".docx"}:
            raise ValueError("Use a PDF or DOCX file")
        config = config or ChunkConfig()
        identity = uuid4().hex
        directory = self.root / identity
        directory.mkdir(mode=0o700)
        source = directory / ("source" + extension)
        digest = hashlib.sha256()
        try:
            with source.open("xb") as output:
                source.chmod(0o600)
                remaining = size
                while remaining:
                    part = stream.read(min(1024 * 1024, remaining))
                    if not part:
                        raise ValueError("Upload ended before the declared file size")
                    output.write(part)
                    digest.update(part)
                    remaining -= len(part)
                output.flush()
                os.fsync(output.fileno())
            with self.connection() as database:
                database.execute(
                    """INSERT INTO documents
                    (id,name,format,size_bytes,identity,config,signature,status,stage,created_at)
                    VALUES (?,?,?,?,?,?,?,'queued','queued',?)""",
                    (
                        identity,
                        name,
                        extension[1:],
                        size,
                        digest.hexdigest(),
                        json.dumps(asdict(config)),
                        self.signature,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except Exception:
            source.unlink(missing_ok=True)
            directory.rmdir()
            raise
        self.wake.set()
        return self.get(identity)

    def add_file(self, path, config=None):
        path = Path(path)
        with path.open("rb") as stream:
            return self.upload(stream, path.stat().st_size, path.name, config)

    def _claim(self):
        with self.connection() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM documents WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row:
                database.execute(
                    "UPDATE documents SET status='processing',stage='starting' WHERE id=?",
                    (row["id"],),
                )
        return dict(row) if row else None

    def _schedule(self):
        pending = set()
        while not self.closed.is_set():
            pending = {future for future in pending if not future.done()}
            while len(pending) < self.workers and not self.closed.is_set():
                job = self._claim()
                if not job:
                    break
                pending.add(self.pool.submit(self._execute, job))
            self.wake.wait(0.1)
            self.wake.clear()

    @staticmethod
    def kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def _execute(self, job):
        started = time.monotonic()
        directory = self.root / job["id"]
        request = {
            "source": str(directory / ("source." + job["format"])),
            "output": str(directory / "result"),
            "format": job["format"],
            "identity": job["identity"],
            "chunks": json.loads(job["config"]),
            "max_file_bytes": self.max_file_bytes,
        }
        error = {"code": "worker_crashed", "message": "Document worker stopped unexpectedly"}
        last_update = 0
        try:
            with (
                tempfile.TemporaryDirectory(prefix="aegis-workspace-") as temporary,
                subprocess.Popen(
                    [sys.executable, "-m", "aegis.workspace.worker"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={**os.environ, "AEGIS_OCR_TEMP_ROOT": temporary},
                ) as process,
            ):
                process.stdin.write(json.dumps(request).encode())
                process.stdin.close()
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                buffer = b""
                ended = False
                try:
                    while not ended:
                        if self.closed.is_set():
                            self.kill(process)
                            self.update(job["id"], status="queued", stage="queued", done=0, total=0)
                            return
                        if time.monotonic() - started > self.timeout:
                            self.kill(process)
                            error = {
                                "code": "timeout",
                                "message": "Document job exceeded its time limit",
                            }
                            break
                        for key, _ in selector.select(0.1):
                            part = os.read(key.fileobj.fileno(), 8192)
                            if not part:
                                ended = True
                                break
                            buffer += part
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                if line.startswith(b"AEGIS_EVENT:"):
                                    event = json.loads(line[12:])
                                    now = time.monotonic()
                                    if (
                                        now - last_update > 0.2
                                        or event["stage"] != job["stage"]
                                        or event["done"] == event["total"]
                                    ):
                                        self.update(job["id"], **event)
                                        last_update = now
                                        job["stage"] = event["stage"]
                                elif line.startswith(b"AEGIS_ERROR:"):
                                    error = json.loads(line[12:])
                            if len(buffer) > 65536:
                                buffer = b""
                    process.wait(timeout=5)
                finally:
                    selector.close()
                if process.returncode == 0:
                    summary = self.read_json(job["id"], "summary.json", ready=False)
                    self.update(
                        job["id"],
                        status="ready",
                        stage="ready",
                        pages=summary["pages"],
                        chunks=summary["chunks"],
                        done=summary["chunks"],
                        total=summary["chunks"],
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        peak_rss_mib=summary.get("peak_rss_mib"),
                        error=None,
                        error_code=None,
                    )
                    return
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError):
            error = {"code": "worker_error", "message": "Document job could not complete"}
        self.update(
            job["id"],
            status="failed",
            stage="failed",
            error_code=error["code"],
            error=error["message"],
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def read_json(self, identity, name, *, ready=True):
        job = self.get(identity)
        if ready and job["status"] != "ready":
            raise ValueError("Document is not ready")
        return json.loads((self.root / identity / "result" / name).read_text())

    def retry(self, identity):
        job = self.get(identity)
        if job["status"] != "failed":
            raise ValueError("Only failed jobs can be retried")
        self.update(
            identity,
            status="queued",
            stage="queued",
            done=0,
            total=0,
            error=None,
            error_code=None,
            signature=self.signature,
        )
        self.wake.set()
        return self.get(identity)

    def remove(self, identity):
        job = self.get(identity)
        if job["status"] == "processing":
            raise ValueError("Wait for processing to finish before removing this document")
        self.update(identity, status="removed", stage="removed")
        # Soft removal preserves data and provenance on disk.

    def image(self, identity, number):
        job = self.get(identity)
        if job["status"] != "ready" or job["format"] != "pdf" or not 1 <= number <= job["pages"]:
            raise ValueError("PDF page is unavailable")
        result = self.root / identity / "result"
        destination = result / f"preview-{number}.png"
        if destination.is_file():
            return destination
        if not self.preview_slots.acquire(timeout=5):
            raise ValueError("Preview busy; try again")
        temporary = result / (uuid4().hex + ".png")
        try:
            request = {
                "action": "preview",
                "source": str(self.root / identity / "source.pdf"),
                "page": number,
                "output": str(temporary),
            }
            with subprocess.Popen(
                [sys.executable, "-m", "aegis.workspace.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as process:
                try:
                    process.communicate(json.dumps(request).encode(), timeout=20)
                except subprocess.TimeoutExpired:
                    self.kill(process)
                    raise ValueError("Page preview timed out") from None
                if process.returncode:
                    raise ValueError("Could not render this page")
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
            self.preview_slots.release()

    def close(self):
        self.closed.set()
        self.wake.set()
        self.coordinator.join(timeout=5)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.lock.close()
