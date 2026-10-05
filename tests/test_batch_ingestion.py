"""Verify durable resume and real subprocess isolation using generated PDFs."""

import fcntl
import json
import subprocess
from pathlib import Path

import pymupdf
import pytest

from aegis.ingestion.batch import BatchConfig, _pipeline_id, _process, _worker, run_batch


def write_pdf(path: Path, text: str = "Engine pressure 42 kPa") -> Path:
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), text)
        document.save(path)
    return path


def test_batch_isolates_failures_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "inputs"
    source.mkdir()
    write_pdf(source / "first.pdf")
    write_pdf(source / "second.PDF", "Temperature 300 K")
    (source / "broken.pdf").write_text("Not a PDF")
    (source / "ignored.docx").write_text("Not supported here")
    output = tmp_path / "output"
    first = run_batch(source, output, BatchConfig(workers=2))
    assert (first["total"], first["succeeded"], first["failed"]) == (3, 2, 1)
    assert (output / "manifest.sqlite3").is_file()
    for job in first["jobs"]:
        if job["status"] == "succeeded":
            record = json.loads(Path(job["output_path"]).read_text())
            assert record["document"]["document_id"] == job["content_sha256"]
            assert record["document"]["source_path"] == job["source_path"]
    resumed = run_batch(source, output)
    assert (resumed["succeeded"], resumed["cached"], resumed["failed"]) == (0, 2, 1)
    assert any(job["outcome"] == "failed_cached" for job in resumed["jobs"])
    assert resumed["pipeline_id"] == first["pipeline_id"]
    assert first["id"] != resumed["id"]


def test_changed_content_creates_another_record(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf")
    output = tmp_path / "output"
    first = run_batch(path, output)
    write_pdf(path, "Changed result")
    second = run_batch(path, output)
    assert second["succeeded"] == 1
    assert first["jobs"][0]["content_sha256"] != second["jobs"][0]["content_sha256"]
    assert len(list((output / "records").glob("*.json"))) == 2


def test_corrupt_or_missing_output_is_rebuilt(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf")
    output = tmp_path / "output"
    first = run_batch(path, output)
    saved = Path(first["jobs"][0]["output_path"])
    saved.write_text("Corrupted output")
    repaired = run_batch(path, output)
    assert repaired["succeeded"] == 1
    assert json.loads(saved.read_text())["schema_version"] == 1
    saved.unlink()
    assert run_batch(path, output)["succeeded"] == 1


def test_pipeline_mode_invalidates_cache_and_persists_maps(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf")
    output = tmp_path / "output"
    baseline = run_batch(path, output)
    normalized = run_batch(path, output, BatchConfig(mode="normalized"))
    assert normalized["succeeded"] == 1
    assert normalized["pipeline_id"] != baseline["pipeline_id"]
    record = json.loads(Path(normalized["jobs"][0]["output_path"]).read_text())
    page = record["document"]["pages"][0]
    assert page["normalized"]["text"] == "Engine pressure 42 kPa"
    assert page["normalized"]["page_mappings"]
    assert page["layout"]["blocks"]


def test_timeout_can_be_retried_explicitly(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf")
    output = tmp_path / "output"
    failed = run_batch(path, output, BatchConfig(timeout_seconds=0.00001))
    assert failed["jobs"][0]["error_code"] == "timeout"
    assert run_batch(path, output)["jobs"][0]["outcome"] == "failed_cached"
    retried = run_batch(path, output, BatchConfig(retry_failed=True))
    assert retried["succeeded"] == 1


def test_changed_source_is_not_written(tmp_path: Path) -> None:
    path = write_pdf(tmp_path / "report.pdf")
    destination = tmp_path / "record.json"
    config = BatchConfig()
    result = _worker(str(path), "wrong-identity", str(destination), _pipeline_id(config), config)
    assert result["error_code"] == "source_changed"
    assert not destination.exists()


def test_native_crash_becomes_a_failed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], -11)
    )
    assert (
        _process("source", "identity", "output", "pipeline", BatchConfig())["error_code"]
        == "worker_crashed"
    )


def test_output_lock_prevents_concurrent_coordinators(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    with (output / ".coordinator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="Another ingestion run"):
            run_batch(tmp_path, output)


@pytest.mark.parametrize("kwargs", [{"workers": 0}, {"mode": "unknown"}, {"timeout_seconds": 0}])
def test_invalid_configuration(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        BatchConfig(**kwargs)
