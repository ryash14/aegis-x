"""Real HTTP checks for identity, isolation, processing, deletion and recovery."""

import http.cookiejar
import io
import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from zipfile import ZipFile

import pymupdf
import pytest
import uvicorn

from aegis.app.server import Settings, create_app
from aegis.app.store import Store
from aegis.retrieval.dense import DenseIndex
from aegis.retrieval.sparse import SparseIndex

PASSWORD = "a deliberately long test password"


class Client:
    def __init__(self, base):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.token = None
        self.project = None

    def request(self, path, *, method="GET", body=None, raw=None, headers=None):
        supplied = dict(headers or {})
        if body is not None:
            raw = json.dumps(body).encode()
            supplied.setdefault("Content-Type", "application/json")
        if self.token and method != "GET":
            supplied.setdefault("X-Aegis-Token", self.token)
        if self.project:
            supplied.setdefault("X-Aegis-Project", self.project)
        request = urllib.request.Request(self.base + path, raw, supplied, method=method)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            data = (
                json.loads(payload)
                if "application/json" in response.headers.get("Content-Type", "")
                else payload
            )
            return response.status, data, response.headers

    def login(self, email="alice@example.test", password=PASSWORD):
        status, challenge, _ = self.request("/api/auth/challenge")
        assert status == 200
        result = self.request(
            "/api/auth/login",
            method="POST",
            body={"email": email, "password": password},
            headers={"X-Aegis-Login": challenge["token"]},
        )
        if result[0] == 200:
            self.token = self.request("/api/session")[1]["token"]
        return result

    def create_project(self, name="Mission documents"):
        status, project, _ = self.request("/api/projects", method="POST", body={"name": name})
        assert status == 201, project
        self.project = project["id"]
        return project

    def upload(self, payload, name="specification.pdf", **headers):
        return self.request(
            "/api/documents",
            method="POST",
            raw=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": name,
                **headers,
            },
        )


def pdf_bytes(pages=2, encrypted=False):
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page().insert_text(
                (50, 50), "REQ-001: Controller operating limit is 85 Celsius."
            )
        options = (
            {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "reader"}
            if encrypted
            else {}
        )
        return document.tobytes(**options)


def docx_bytes():
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Mission subsystem requirements</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Temperature</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>85 Celsius</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
        )
    return result.getvalue()


def scan_bytes():
    with pymupdf.open() as native:
        page = native.new_page(width=600, height=250)
        page.insert_text((35, 80), "Mission controller operating temperature", fontsize=22)
        page.insert_text((35, 125), "The maximum limit is 85 Celsius.", fontsize=22)
        raster = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    with pymupdf.open() as scan:
        page = scan.new_page(width=600, height=250)
        page.insert_image(page.rect, stream=raster)
        return scan.tobytes()


def until(condition, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("Operation did not complete in time")


@contextmanager
def running(root, **overrides):
    settings = Settings(
        storage=root, workers=1, max_file_bytes=2 * 1024**2, job_timeout=30, **overrides
    )
    app = create_app(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        until(lambda: server.started, timeout=15)
        yield app, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive(), "Application failed to stop its workers"


@pytest.fixture
def private_app(tmp_path):
    root = tmp_path / "app"
    store = Store(root)
    store.add_user("alice@example.test", "Alice", PASSWORD)
    store.add_user("bob@example.test", "Bob", PASSWORD)
    with running(root) as (app, base):
        alice, bob = Client(base), Client(base)
        assert alice.login()[0] == 200
        assert bob.login("bob@example.test")[0] == 200
        yield app, alice, bob


def ready(client, identity, expected="ready"):
    def complete():
        status, job, _ = client.request("/api/documents/" + identity)
        return job if status == 200 and job["status"] in {"ready", "failed"} else None

    job = until(complete, 30)
    assert job["status"] == expected, job
    return job


def test_identity_cookie_csrf_expiry_and_revocation(private_app):
    app, alice, _ = private_app
    anonymous = Client(alice.base)
    for path in ["/api/session", "/api/projects", "/api/documents/unknown/original"]:
        assert anonymous.request(path)[0] == 401
    assert anonymous.request("/api/projects", method="POST", body={"name": "Denied"})[0] == 401
    assert (
        alice.request(
            "/api/projects",
            method="POST",
            body={"name": "Denied"},
            headers={"X-Aegis-Token": "wrong"},
        )[0]
        == 403
    )
    assert alice.request("/api/session", headers={"Origin": "https://attacker.test"})[0] == 403
    assert alice.request("/api/session", headers={"Host": "attacker.test"})[0] == 400
    assert (
        anonymous.request(
            "/api/auth/login",
            method="POST",
            body={"email": "alice@example.test", "password": PASSWORD},
        )[0]
        == 403
    )
    _, _, headers = alice.login()
    cookie = headers.get_all("Set-Cookie")[0]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    with app.state.store.connection() as db:
        session_tokens = [r[0] for r in db.execute("SELECT token_hash FROM sessions")]
    actual = next(c.value for c in alice.cookies if c.name == "aegis_session")
    assert actual not in session_tokens
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert alice.request("/api/auth/logout", method="POST")[0] == 200
    assert alice.request("/api/session")[0] == 401
    assert alice.login()[0] == 200
    with app.state.store.connection() as db:
        db.execute(
            "UPDATE sessions SET expires_at=0 WHERE user_id="
            "(SELECT id FROM users WHERE email='alice@example.test')"
        )
    assert alice.request("/api/session")[0] == 401
    assert alice.login()[0] == 200
    app.state.store.disable_user("alice@example.test")
    assert alice.request("/api/session")[0] == 401
    assert alice.login()[0] == 401


def test_project_isolation_and_all_artifact_routes(private_app):
    _, alice, bob = private_app
    project = alice.create_project()
    other = bob.create_project("Bob's project")
    status, uploaded, _ = alice.upload(pdf_bytes())
    assert status == 201
    job = ready(alice, uploaded["id"])
    assert alice.request("/api/documents")[1]["total"] == 1
    assert bob.request("/api/documents")[1]["total"] == 0
    assert bob.request("/api/projects")[1]["projects"][0]["id"] == other["id"]
    for method, path in [
        ("PATCH", "/api/projects/" + project["id"]),
        ("DELETE", "/api/projects/" + project["id"]),
        *[
            ("GET", f"/api/documents/{job['id']}" + suffix)
            for suffix in [
                "",
                "/original",
                "/summary",
                "/pages/1",
                "/pages/1/image",
                "/chunks",
                "/chunks/0",
                "/structure",
            ]
        ],
        ("POST", f"/api/documents/{job['id']}/retry"),
        ("POST", f"/api/documents/{job['id']}/remove"),
        ("PATCH", f"/api/documents/{job['id']}"),
    ]:
        assert (
            bob.request(
                path,
                method=method,
                body={"name": "Intrusion", "revision": "Bad"} if method == "PATCH" else None,
            )[0]
            == 404
        ), path
    assert bob.request("/api/documents", headers={"X-Aegis-Project": project["id"]})[0] == 404
    assert bob.upload(pdf_bytes(), **{"X-Aegis-Project": project["id"]})[0] == 404
    assert alice.request(f"/api/documents/{job['id']}/original", headers={"Range": "bytes=0-3"})[
        :2
    ] == (206, b"%PDF")
    assert (
        alice.request(f"/api/documents/{job['id']}/original")[2]["X-Frame-Options"] == "SAMEORIGIN"
    )
    assert alice.request(f"/api/documents/{job['id']}/pages/1/image")[1].startswith(b"\x89PNG")
    assert (
        alice.request(f"/api/documents/{job['id']}/summary")[1]["coverage"]["status"] == "complete"
    )
    chunk = alice.request(f"/api/documents/{job['id']}/chunks/0")[1]
    assert chunk["mappings"] and "85 Celsius" in chunk["text"]
    second = alice.create_project("Separate project")
    assert alice.request("/api/documents")[1]["total"] == 0
    assert alice.request(f"/api/documents/{job['id']}")[0] == 404
    assert second["id"] != project["id"]


def test_pdf_docx_ocr_and_isolated_failure(private_app):
    _, alice, _ = private_app
    alice.create_project()
    uploads = [
        alice.upload(pdf_bytes())[1],
        alice.upload(docx_bytes(), "table.docx")[1],
        alice.upload(scan_bytes(), "scan.pdf")[1],
        alice.upload(pdf_bytes(encrypted=True), "locked.pdf")[1],
    ]
    jobs = [
        ready(alice, item["id"], "failed" if index == 3 else "ready")
        for index, item in enumerate(uploads)
    ]
    structure = alice.request(f"/api/documents/{jobs[1]['id']}/structure")[1]
    assert any(item["kind"] == "table" for item in structure["blocks"])
    scanned = alice.request(f"/api/documents/{jobs[2]['id']}/pages/1")[1]
    assert scanned["text"] == "" and "operating temperature" in scanned["ocr"]["text"]
    assert jobs[3]["error_code"] == "encrypted_pdf"
    assert alice.request(f"/api/documents/{jobs[3]['id']}/retry", method="POST")[0] == 200
    ready(alice, jobs[3]["id"], "failed")
    assert alice.request("/api/documents")[1]["states"] == {"failed": 1, "ready": 3}
    assert (
        alice.request("/api/documents", headers={"X-Aegis-Project": alice.project})[1]["total"] == 4
    )


def test_actual_deletion_cascades_indexes_and_project(private_app):
    app, alice, _ = private_app
    project = alice.create_project()
    uploaded = alice.upload(
        pdf_bytes(), **{"X-Aegis-Revision": "Rev%20%CE%B1", "X-Aegis-Role": "baseline"}
    )[1]
    job = ready(alice, uploaded["id"])
    assert job["revision"] == "Rev α" and job["role"] == "baseline"
    changed = alice.request(
        f"/api/documents/{job['id']}",
        method="PATCH",
        body={"revision": "Rev B", "role": "candidate"},
    )[1]
    assert changed["identity"] == job["identity"] and changed["role"] == "candidate"
    workspace = app.state.workspace
    sparse = SparseIndex(workspace.db)
    sparse.sync_workspace(workspace)
    dense = DenseIndex(workspace.db, SimpleNamespace(key="test", dimension=2))
    with sparse.connection() as db:
        row = db.execute("SELECT rowid FROM retrieval_chunks LIMIT 1").fetchone()[0]
        db.execute("INSERT INTO dense_vectors VALUES(?,?,?,?,?)", (row, "test", 0, "{}", b"\0" * 8))
    assert sparse.search("Controller")
    assert alice.request(f"/api/documents/{job['id']}", method="DELETE")[0] == 200
    assert not (workspace.root / job["id"]).exists()
    assert not sparse.search("Controller") and dense.stats()["windows"] == 0
    assert alice.request(f"/api/documents/{job['id']}/original")[0] == 404
    with workspace.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM documents WHERE id=?", (job["id"],)).fetchone()[0] == 0
        )
    next_job = alice.upload(docx_bytes(), "other.docx")[1]
    ready(alice, next_job["id"])
    assert alice.request("/api/projects/" + project["id"], method="DELETE")[0] == 200
    assert not (workspace.root / next_job["id"]).exists()
    assert not alice.request("/api/projects")[1]["projects"]


def test_delete_queued_and_active_jobs_without_revival(private_app):
    app, alice, _ = private_app
    alice.create_project()
    active = alice.upload(pdf_bytes(250))[1]
    process = until(lambda: app.state.workspace.processes.get(active["id"]))
    queued = alice.upload(pdf_bytes())[1]
    assert queued["status"] == "queued"
    assert alice.request(f"/api/documents/{queued['id']}", method="DELETE")[0] == 200
    assert alice.request(f"/api/documents/{active['id']}", method="DELETE")[0] == 200
    assert process.poll() is not None
    until(lambda: not app.state.workspace.processes)
    for job in (active, queued):
        assert alice.request(f"/api/documents/{job['id']}")[0] == 404
        assert not (app.state.workspace.root / job["id"]).exists()
    assert alice.request("/api/documents")[1]["total"] == 0


def test_validation_size_and_login_throttling(private_app):
    app, alice, _ = private_app
    alice.create_project()
    for payload, name in [(b"", "empty.pdf"), (b"bad", "bad.txt")]:
        assert alice.upload(payload, name)[0] in {400, 413}
    assert alice.upload(pdf_bytes(), **{"X-Aegis-Config": '{"max_chars":true}'})[0] == 400
    assert alice.upload(b"x" * (app.state.settings.max_file_bytes + 1))[0] == 413
    assert alice.request("/api/documents")[1]["total"] == 0
    for _ in range(4):
        assert app.state.upload_slots.acquire(blocking=False)
    try:
        assert alice.upload(pdf_bytes())[0] == 429
    finally:
        for _ in range(4):
            app.state.upload_slots.release()
    assert alice.request("/api/projects", method="PATCH", body={"name": "Wrong route"})[0] == 405
    assert (
        alice.request(
            "/api/projects/" + alice.project,
            method="PATCH",
            body={"name": "Multiline project", "description": "First line\nSecond line"},
        )[0]
        == 200
    )
    anonymous = Client(alice.base)
    for _ in range(6):
        assert anonymous.login("missing@example.test", "incorrect")[0] == 401
    assert anonymous.login("missing@example.test", "incorrect")[0] == 429
    assert (
        alice.request(
            "/api/projects", method="POST", body={"name": "x", "description": "x" * 20000}
        )[0]
        == 413
    )
    assert alice.request("/api/documents")[1]["total"] == 0


def test_persistence_requeues_interrupted_job_and_keeps_session(tmp_path):
    root = tmp_path / "app"
    Store(root).add_user("alice@example.test", "Alice", PASSWORD)
    with running(root) as (app, base):
        client = Client(base)
        client.login()
        client.create_project()
        completed = client.upload(docx_bytes(), "table.docx")[1]
        ready(client, completed["id"])
        interrupted = client.upload(pdf_bytes(250))[1]
        until(lambda: app.state.workspace.processes.get(interrupted["id"]))
    with running(root) as (app, base):
        client.base = base
        assert client.request("/api/session")[0] == 200
        assert client.request("/api/documents")[1]["total"] == 2
        assert client.request(f"/api/documents/{completed['id']}/structure")[0] == 200
        ready(client, interrupted["id"])


def test_interrupted_delete_and_unmapped_upload_recovered(tmp_path):
    root = tmp_path / "app"
    Store(root).add_user("alice@example.test", "Alice", PASSWORD)
    with running(root) as (app, base):
        client = Client(base)
        client.login()
        project = client.create_project()
        job = client.upload(pdf_bytes())[1]
        ready(client, job["id"])
        app.state.store.begin_delete(client.request("/api/session")[1]["user"]["id"], project["id"])
        orphan = app.state.workspace.upload(io.BytesIO(pdf_bytes()), len(pdf_bytes()), "orphan.pdf")
    with running(root) as (app, base):
        client.base = base
        assert client.request("/api/projects")[1]["projects"] == []
        assert not (app.state.workspace.root / job["id"]).exists()
        assert not (app.state.workspace.root / orphan["id"]).exists()


def test_schema_and_secure_cookie_configuration(tmp_path):
    root = tmp_path / "app"
    store = Store(root)
    store.add_user("alice@example.test", "Alice", PASSWORD)
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT password_hash FROM users").fetchone()[0].startswith("$argon2id$")
    with running(root, secure_cookies=True) as (_, base):
        client = Client(base)
        _, challenge, headers = client.request("/api/auth/challenge")
        value = challenge["token"]
        assert "Secure" in headers["Set-Cookie"] and "__Host-aegis_login" in headers["Set-Cookie"]
        status, _, headers = client.request(
            "/api/auth/login",
            method="POST",
            body={"email": "alice@example.test", "password": PASSWORD},
            headers={"Cookie": "__Host-aegis_login=" + value, "X-Aegis-Login": value},
        )
        assert status == 200 and "__Host-aegis_session" in headers.get_all("Set-Cookie")[0]
        assert "Secure" in headers.get_all("Set-Cookie")[0]
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="newer"):
        Store(root)
    with pytest.raises(ValueError):
        Settings(public_origin="https://research.test", allowed_hosts=("research.test",))


def test_password_change_revokes_sessions_and_preserves_projects(private_app):
    app, alice, _ = private_app
    project = alice.create_project()
    app.state.store.set_password("alice@example.test", "replacement test password")
    assert alice.request("/api/session")[0] == 401
    assert alice.login()[0] == 401
    assert alice.login(password="replacement test password")[0] == 200
    assert alice.request("/api/projects")[1]["projects"][0]["id"] == project["id"]
    with pytest.raises(ValueError, match="User not found"):
        app.state.store.set_password("missing@example.test", "replacement test password")


def test_project_search_filters_injection_deletion_and_bounded_context(private_app):
    app, alice, bob = private_app
    alice.create_project()
    identity = alice.upload(pdf_bytes(), **{"X-Aegis-Revision": "A", "X-Aegis-Role": "baseline"})[
        1
    ]["id"]
    ready(alice, identity)
    app.state.research.sync()
    status, report, _ = alice.request("/api/search?q=controller&mode=sparse&budget=1000")
    assert status == 200 and report["results"]
    assert report["context_chars"] <= report["context_budget"]
    assert all(hit["job_id"] == identity for hit in report["results"])
    assert all(hit["chunk"]["mappings"] for hit in report["results"])
    assert "chunk=" in report["results"][0]["source_url"]
    assert alice.request("/api/search?q=controller&mode=sparse&role=candidate")[1]["results"] == []
    assert alice.request("/api/search?q=controller&mode=sparse&revision=A")[1]["results"]
    assert alice.request("/api/search?q=controller&mode=sparse&format=docx")[1]["results"] == []
    assert alice.request("/api/search?q=controller&mode=sparse&kind=table")[1]["results"] == []
    assert alice.request("/api/search?q=controller&mode=sparse&section=absent")[1]["results"] == []
    bob.create_project()
    assert bob.request("/api/search?q=controller&mode=sparse")[1]["results"] == []
    assert bob.request("/api/search/status")[1]["documents"] == []
    assert bob.request("/api/search?q=controller&mode=sparse&document_id=" + identity)[0] == 404
    assert (
        alice.request("/api/search?q=%27%3B%20DROP%20TABLE%20users%3B%20--&mode=sparse")[0] == 200
    )
    assert (
        alice.request("/api/search?q=controller&mode=sparse&revision=%27%20OR%201%3D1--")[1][
            "results"
        ]
        == []
    )
    assert alice.request("/api/search?q=controller&mode=wrong")[0] == 400
    assert alice.request("/api/search?q=controller&mode=sparse&budget=999")[0] == 400
    assert alice.request("/api/search?q=controller&mode=sparse&page=-1")[0] == 400
    for _ in range(2):
        assert app.state.research.query_slots.acquire(blocking=False)
    try:
        assert alice.request("/api/search?q=controller&mode=sparse")[0] == 429
    finally:
        for _ in range(2):
            app.state.research.query_slots.release()
    assert alice.request("/api/documents/" + identity, method="DELETE")[0] == 200
    app.state.research.sync()
    assert alice.request("/api/search?q=controller&mode=sparse")[1]["results"] == []
    assert alice.request("/api/search/status")[1]["documents"] == []


def test_injection_login_csrf_and_path_traversal_fail_closed(private_app):
    app, alice, _ = private_app
    alice.create_project()
    assert Client(alice.base).login("' OR '1'='1@example.test")[0] in {400, 401}
    assert (
        alice.request(
            "/api/projects",
            method="POST",
            body={"name": "intrusion"},
            headers={"X-Aegis-Token": "é"},
        )[0]
        == 403
    )
    assert alice.request("/api/documents/../../app.sqlite3")[0] == 404
    assert alice.request("/assets/../session.key")[0] == 404
    assert alice.request("/api/projects", headers={"Host": "localhost/evil"})[0] == 400
    with app.state.store.connection() as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2


def test_search_persists_across_restart(tmp_path):
    root = tmp_path / "app"
    Store(root).add_user("alice@example.test", "Alice", PASSWORD)
    with running(root) as (app, base):
        alice = Client(base)
        alice.login()
        project = alice.create_project()
        job = alice.upload(pdf_bytes())[1]
        ready(alice, job["id"])
        app.state.research.sync()
        assert alice.request("/api/search?q=controller&mode=sparse")[1]["results"]
    with running(root) as (_, base):
        alice.base = base
        alice.project = project["id"]
        assert alice.request("/api/search?q=controller&mode=sparse")[1]["results"]


def test_exact_chunk_focus_beyond_first_page(private_app):
    _, alice, _ = private_app
    alice.create_project()
    content = io.BytesIO()
    with ZipFile(content, "w") as archive:
        body = "".join(
            f"<w:p><w:r><w:t>REQ-{number:03}: Controller operating threshold shall hold.</w:t>"
            "</w:r></w:p>"
            for number in range(80)
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
    job = alice.upload(
        content.getvalue(),
        "many-requirements.docx",
        **{"X-Aegis-Config": '{"max_chars":32,"overlap_chars":0}'},
    )[1]
    ready(alice, job["id"])
    status, chunks, _ = alice.request(f"/api/documents/{job['id']}/chunks?focus=60")
    assert status == 200 and chunks["offset"] == 50
    assert any(chunk["index"] == 60 for chunk in chunks["chunks"])
    assert alice.request(f"/api/documents/{job['id']}/chunks?focus=999999")[0] == 404
