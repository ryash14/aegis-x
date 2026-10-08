"""Real HTTP uploads, isolated parsing, persistent jobs and local request boundaries."""

import io
import json
import threading
import time
import urllib.error
import urllib.request
from zipfile import ZipFile

import pymupdf
import pytest

from aegis.workspace.server import LocalServer
from aegis.workspace.service import Workspace


def pdf_bytes():
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 50), "Engine pressure 42 kPa")
        document.new_page().insert_text((40, 50), "Temperature stable")
        return document.tobytes()


def docx_bytes():
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Engine report</w:t></w:r></w:p>"
                "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>42 kPa</w:t></w:r></w:p>"
                "</w:tc></w:tr></w:tbl></w:body></w:document>"
            ),
        )
    return result.getvalue()


@pytest.fixture
def app(tmp_path):
    workspace = Workspace(tmp_path / "storage", workers=2, max_file_bytes=100 * 1024, timeout=30)
    server = LocalServer(("127.0.0.1", 0), workspace, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, workspace, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    workspace.close()


def request(base, path, *, payload=None, headers=None, method=None):
    req = urllib.request.Request(base + path, payload, headers or {}, method=method)
    return urllib.request.urlopen(req)


def upload(server, base, payload, name, settings=None):
    headers = {
        "X-Aegis-Token": server.token,
        "X-Filename": name,
        "Content-Type": "application/octet-stream",
    }
    if settings:
        headers["X-Aegis-Config"] = json.dumps(settings)
    return json.load(request(base, "/api/documents", payload=payload, headers=headers))


def wait(workspace, ids, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [workspace.get(identity) for identity in ids]
        if all(job["status"] in {"ready", "failed"} for job in jobs):
            return jobs
        time.sleep(0.05)
    pytest.fail("Workspace jobs did not finish")


def test_real_upload_methods_chunks_and_byte_ranges(app):
    server, workspace, base = app
    job = upload(server, base, pdf_bytes(), "engine.pdf", {"max_chars": 100, "overlap_chars": 10})
    finished = wait(workspace, [job["id"]])[0]
    assert finished["status"] == "ready"
    assert finished["pages"] == 2 and finished["chunks"] == 2
    summary = json.load(request(base, f"/api/documents/{job['id']}/summary"))
    assert summary["coverage"]["status"] == "complete"
    assert summary["peak_rss_mib"] > 0
    assert summary["chunk_config"]["max_chars"] == 100
    page = json.load(request(base, f"/api/documents/{job['id']}/pages/1"))
    assert "42 kPa" in page["text"] and page["layout"] and page["normalized"]
    chunks = json.load(request(base, f"/api/documents/{job['id']}/chunks?page=1"))
    assert chunks["total"] == 1
    chunk = json.load(request(base, f"/api/documents/{job['id']}/chunks/0"))
    assert chunk["mappings"][0]["sources"][0]["page"] == 1
    with request(
        base, f"/api/documents/{job['id']}/original", headers={"Range": "bytes=0-4"}
    ) as response:
        assert response.status == 206 and response.read() == b"%PDF-"
    with request(base, f"/api/documents/{job['id']}/pages/2/image") as response:
        assert response.read().startswith(b"\x89PNG")


def test_docx_and_failed_pdf_are_independent_and_retryable(app):
    server, workspace, base = app
    jobs = [
        upload(server, base, docx_bytes(), "engine.docx"),
        upload(server, base, b"not a PDF", "broken.pdf"),
    ]
    good, bad = wait(workspace, [job["id"] for job in jobs])
    assert good["status"] == "ready" and good["pages"] == 0
    structure = json.load(request(base, f"/api/documents/{good['id']}/structure"))
    assert structure["blocks"][1]["kind"] == "table"
    assert bad["status"] == "failed" and bad["error_code"] == "invalid_pdf"
    request(
        base,
        f"/api/documents/{bad['id']}/retry",
        payload=b"",
        headers={"X-Aegis-Token": server.token},
    )
    assert wait(workspace, [bad["id"]])[0]["status"] == "failed"
    request(
        base,
        f"/api/documents/{good['id']}/remove",
        payload=b"",
        headers={"X-Aegis-Token": server.token},
    )
    assert (workspace.root / good["id"] / "source.docx").is_file()
    assert json.load(request(base, "/api/documents"))["total"] == 1


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Aegis-Token": "wrong"},
        {"Origin": "https://external.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_external_or_unauthenticated_mutations_rejected(app, headers):
    server, _, base = app
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(base, "/api/documents", payload=b"data", headers=headers)
    assert caught.value.code == 403


def test_host_guard_oversize_unsupported_and_path_routes(app):
    server, workspace, base = app
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(base, "/api/session", headers={"Host": "external.example"})
    assert caught.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as caught:
        upload(server, base, b"X" * (workspace.max_file_bytes + 1), "large.pdf")
    assert caught.value.code == 413
    with pytest.raises(urllib.error.HTTPError) as caught:
        upload(server, base, b"data", "file.exe")
    assert caught.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as caught:
        request(base, "/data/workspace/workspace.sqlite3")
    assert caught.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        request(base, "/docs/../../pyproject.toml")
    assert json.load(request(base, "/api/documents"))["total"] == 0


def test_search_page_is_packaged_without_project_docs(app):
    server, _, base = app
    assert not (server.project / "docs").exists()
    for route in ("/search", "/docs/retrieval.html"):
        with request(base, route) as response:
            assert response.headers["Content-Type"] == "text/html; charset=utf-8"
            assert b"Find evidence." in response.read()
    with request(base, "/") as response:
        assert b'href="/search"' in response.read()


def test_short_upload_never_becomes_a_job(app):
    _, workspace, _ = app
    with pytest.raises(ValueError, match="before the declared"):
        workspace.upload(io.BytesIO(b"short"), 100, "short.pdf")
    assert workspace.list()["total"] == 0
    assert not list(workspace.root.glob("*/source.pdf"))


def test_pagination_has_no_collection_count_cap(app):
    server, workspace, base = app
    payload = pdf_bytes()
    # Metadata pagination is independent of parser concurrency; use ordinary uploads.
    jobs = [
        workspace.upload(io.BytesIO(payload), len(payload), f"document-{index}.pdf")
        for index in range(7)
    ]
    first = json.load(request(base, "/api/documents?limit=3"))
    second = json.load(request(base, "/api/documents?limit=3&offset=3"))
    assert first["total"] == 7
    assert len(first["documents"]) == len(second["documents"]) == 3
    assert not {job["id"] for job in first["documents"]} & {
        job["id"] for job in second["documents"]
    }
    assert len(workspace.list(query="document-6")["documents"]) == 1
    assert all(job["status"] == "ready" for job in wait(workspace, [job["id"] for job in jobs]))


def test_restart_preserves_ready_jobs_and_recovers_interrupted_jobs(tmp_path):
    root = tmp_path / "storage"
    workspace = Workspace(root, workers=1)
    payload = pdf_bytes()
    job = workspace.upload(io.BytesIO(payload), len(payload), "report.pdf")
    assert wait(workspace, [job["id"]])[0]["status"] == "ready"
    workspace.close()
    restarted = Workspace(root, workers=1)
    assert restarted.get(job["id"])["status"] == "ready"
    restarted.update(job["id"], status="processing", stage="parsing")
    restarted.close()
    recovered = Workspace(root, workers=1)
    assert wait(recovered, [job["id"]])[0]["status"] == "ready"
    recovered.close()


def test_job_timeout_is_explicit(tmp_path):
    workspace = Workspace(tmp_path, workers=1, timeout=0.0001)
    payload = pdf_bytes()
    job = workspace.upload(io.BytesIO(payload), len(payload), "report.pdf")
    result = wait(workspace, [job["id"]])[0]
    workspace.close()
    assert result["status"] == "failed" and result["error_code"] == "timeout"


def test_same_storage_cannot_have_competing_servers(tmp_path):
    workspace = Workspace(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="Another workspace"):
            Workspace(tmp_path)
    finally:
        workspace.close()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0])
def test_nonfinite_or_nonpositive_timeout_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match="limits"):
        Workspace(tmp_path / "storage", timeout=timeout)
    assert not (tmp_path / "storage").exists()


def test_live_retrieval_refresh_sources_and_removed_job_visibility(app):
    server, workspace, base = app
    job = upload(server, base, pdf_bytes(), "engine.pdf")
    wait(workspace, [job["id"]])
    headers = {"X-Aegis-Token": server.token}
    indexed = json.load(request(base, "/api/retrieval/index", payload=b"", headers=headers))
    assert indexed["documents"] == 1 and indexed["chunks"] == 2
    results = json.load(request(base, "/api/search?q=pressure&mode=all"))["results"]
    assert results[0]["job_id"] == job["id"] and results[0]["pages"] == [1]
    assert results[0]["chunk"]["mappings"][0]["sources"][0]["page"] == 1
    unchanged = json.load(request(base, "/api/retrieval/index", payload=b"", headers=headers))
    assert unchanged["updated_documents"] == 0
    workspace.remove(job["id"])
    assert not json.load(request(base, "/api/search?q=pressure"))["results"]
    pruned = json.load(request(base, "/api/retrieval/index", payload=b"", headers=headers))
    assert pruned["chunks"] == 0


def test_dense_http_filters_and_live_removal(app):
    np = pytest.importorskip("numpy")

    class Encoder:
        key, dimension = "http-test", 2

        def passages(self, text):
            vector = [1, 0] if "pressure" in text else [0, 1]
            return (
                [{"token_start": 0, "token_end": 1, "char_start": 0, "char_end": len(text)}],
                np.array([vector], dtype="<f4"),
            )

        def query(self, text):
            return np.array([1, 0], dtype="<f4")

    server, workspace, base = app
    server.encoder = Encoder()
    job = upload(server, base, pdf_bytes(), "engine.pdf")
    wait(workspace, [job["id"]])
    indexed = json.load(
        request(base, "/api/dense/index", payload=b"", headers={"X-Aegis-Token": server.token})
    )
    assert indexed["chunks"] == 2
    hits = json.load(request(base, "/api/search?q=force&method=dense"))["results"]
    assert hits[0]["pages"] == [1] and hits[0]["score"] == 1
    assert json.load(request(base, "/api/search?q=force&method=dense&page=2"))["results"][0][
        "pages"
    ] == [2]
    workspace.remove(job["id"])
    assert json.load(request(base, "/api/search?q=force&method=dense"))["results"] == []


def test_cancelled_worker_cannot_publish_or_race_with_retry(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path, workers=1)
    started, release = threading.Event(), threading.Event()
    original = workspace._execute

    def blocked(job):
        started.set()
        release.wait(5)
        workspace.update(job["id"], status="ready", stage="complete")

    monkeypatch.setattr(workspace, "_execute", blocked)
    try:
        payload = pdf_bytes()
        job = workspace.upload(io.BytesIO(payload), len(payload), "cancel.pdf")
        assert started.wait(5)
        assert workspace.cancel(job["id"])["status"] == "cancelled"
        with pytest.raises(ValueError, match="stopping"):
            workspace.retry(job["id"])
        release.set()
        deadline = time.monotonic() + 5
        while job["id"] in workspace.active_jobs and time.monotonic() < deadline:
            time.sleep(0.01)
        assert workspace.get(job["id"])["status"] == "cancelled"
        monkeypatch.setattr(workspace, "_execute", original)
        workspace.retry(job["id"])
        assert wait(workspace, [job["id"]])[0]["status"] == "ready"
    finally:
        release.set()
        workspace.close()
