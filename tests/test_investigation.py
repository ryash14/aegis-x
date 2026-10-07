"""Research contracts and real HTTP lifecycle with deterministic model proposals."""

import json
import threading
import time
from uuid import uuid4

import pytest

from aegis.app.investigation import MODEL_DIGEST, Cancelled, LocalModel, validate_answer

from .test_private_app import PASSWORD, Client, Store, pdf_bytes, ready, running, until


class FakeModel:
    model = "qwen3:8b"
    digest = MODEL_DIGEST

    def __init__(self, behavior="valid"):
        self.behavior, self.calls = behavior, []
        self.entered = threading.Event()

    def generate(self, messages, schema, remaining, check):
        self.calls.append(messages)
        self.entered.set()
        if self.behavior == "blocked":
            while True:
                check()
                time.sleep(0.02)
        if self.behavior == "unavailable":
            raise ConnectionError("Local model unavailable")
        if "subquestions" in schema["properties"]:
            output = {"subquestions": ["What is the controller operating limit?"]}
        elif "reviews" in schema["properties"]:
            payload = json.loads(messages[-1]["content"])
            output = {
                "reviews": [
                    {
                        "claim_index": item["index"],
                        "verdict": "unsupported" if self.behavior == "unsupported" else "supported",
                        "reason": "Reviewed against the source.",
                    }
                    for item in payload["claims"]
                ]
            }
        else:
            payload = json.loads(messages[-1]["content"])
            source = payload["evidence"][0]
            output = {
                "claims": [
                    {
                        "text": "The controller operating limit is 85 Celsius.",
                        "subquestion": number,
                        "citations": [
                            {
                                "evidence_id": "unknown"
                                if self.behavior == "invalid"
                                else source["id"],
                                "quote": source["text"],
                            }
                        ],
                    }
                    for number in range(len(payload["questions"]))
                ],
                "unresolved": [],
                "potential_conflicts": [],
            }
        return {
            "output": output,
            "model": self.model,
            "digest": self.digest,
            "prompt_tokens": 100,
            "output_tokens": 100,
        }


@pytest.fixture
def research_app(tmp_path):
    root = tmp_path / "app"
    store = Store(root)
    store.add_user("alice@example.test", "Alice", PASSWORD)
    store.add_user("bob@example.test", "Bob", PASSWORD)
    with running(root) as (app, base):
        alice, bob = Client(base), Client(base)
        alice.login()
        bob.login("bob@example.test")
        alice.create_project()
        job = alice.upload(pdf_bytes())[1]
        ready(alice, job["id"])
        app.state.research.sync()
        app.state.investigations.model = FakeModel()
        yield app, alice, bob, job


def start(client, **kwargs):
    status, run, _ = client.request(
        "/api/research",
        method="POST",
        body={
            "question": "What is the controller operating limit?",
            "retrieval": "sparse",
            **kwargs,
        },
    )
    assert status == 202, run
    return run


def complete(client, identity):
    return until(
        lambda: (
            run
            if (run := client.request("/api/research/" + identity)[1])["status"]
            in {"completed", "cancelled", "failed"}
            else None
        ),
        30,
    )


def test_cited_answer_saved_followup_reports_and_isolation(research_app):
    app, alice, bob, job = research_app
    run = complete(alice, start(alice, mode="investigate")["id"])
    assert run["status"] == "completed", run
    assert run["result"]["status"] == "answered"
    assert len(run["result"]["claims"]) == 2
    assert len(app.state.investigations.model.calls) == 3
    assert all(claim["citations"][0]["reference_validated"] for claim in run["result"]["claims"])
    assert run["evidence"][0]["document_sha256"] == job["identity"]
    assert run["config"]["model_digest"] == MODEL_DIGEST
    assert [step["step"] for step in run["trace"]].count("retrieve") == 2
    assert alice.request("/api/research")[1]["runs"][0]["id"] == run["id"]
    for suffix in ("", "/report", "/evidence/E001"):
        assert bob.request("/api/research/" + run["id"] + suffix)[0] == 404
    for suffix in ("/cancel", "/retry"):
        assert bob.request("/api/research/" + run["id"] + suffix, method="POST")[0] == 404
    report = alice.request("/api/research/" + run["id"] + "/report")
    assert report[0] == 200 and "attachment" in report[2]["Content-Disposition"]
    assert alice.request("/api/research/" + run["id"] + "/evidence/E001")[1]["text"]
    followup = complete(alice, start(alice, parent_id=run["id"])["id"])
    assert followup["status"] == "completed" and followup["parent_id"] == run["id"]
    payload = json.loads(app.state.investigations.model.calls[-1][-1]["content"])
    assert payload["questions"][0] == followup["question"]
    assert alice.request("/api/research/" + run["id"] + "/retry", method="POST")[0] == 409
    assert alice.request("/api/documents/" + job["id"], method="DELETE")[0] == 200
    assert alice.request("/api/research/" + run["id"])[0] == 404
    assert alice.request("/api/research/" + followup["id"])[0] == 404


@pytest.mark.parametrize("behavior", ["invalid", "unsupported"])
def test_invalid_references_and_unsupported_claims_are_not_answers(research_app, behavior):
    app, alice, _, _ = research_app
    app.state.investigations.model = FakeModel(behavior)
    run = complete(alice, start(alice)["id"])
    assert run["status"] == "completed"
    assert run["result"]["status"] == "insufficient_evidence"
    assert run["result"]["claims"] == [] and run["result"]["unresolved"]


def test_cancel_retry_deadline_and_model_unavailable(research_app):
    app, alice, _, _ = research_app
    model = FakeModel("blocked")
    app.state.investigations.model = model
    run = start(alice)
    assert model.entered.wait(10)
    assert (
        alice.request("/api/research/" + run["id"] + "/cancel", method="POST")[1]["status"]
        == "cancelled"
    )
    app.state.investigations.model = FakeModel("unavailable")
    _, retry, _ = alice.request("/api/research/" + run["id"] + "/retry", method="POST")
    failed = complete(alice, retry["id"])
    assert failed["status"] == "failed" and "unavailable" in failed["error"]
    app.state.investigations.timeout = 0.2
    app.state.investigations.model = FakeModel("blocked")
    timed = complete(alice, start(alice)["id"])
    assert timed["status"] == "failed" and "time budget" in timed["error"]


def test_no_evidence_skips_model_and_bad_config_is_rejected(research_app):
    app, alice, bob, _ = research_app
    bob.create_project()
    result = complete(bob, start(bob)["id"])
    assert result["result"]["status"] == "insufficient_evidence"
    assert app.state.investigations.model.calls == []
    for body in ({"mode": []}, {"parent_id": []}, {"retrieval": []}, {"question": "界" * 1000}):
        assert (
            alice.request("/api/research", method="POST", body={"question": "controller", **body})[
                0
            ]
            == 400
        )
    other = start(bob)
    assert (
        alice.request(
            "/api/research",
            method="POST",
            body={"question": "controller", "parent_id": other["id"]},
        )[0]
        == 404
    )
    assert (
        alice.request(
            "/api/research",
            method="POST",
            body={"question": "controller"},
            headers={"X-Aegis-Token": "bad"},
        )[0]
        == 403
    )


def test_restart_keeps_answers_and_marks_interrupted_runs(tmp_path):
    root = tmp_path / "app"
    Store(root).add_user("alice@example.test", "Alice", PASSWORD)
    with running(root) as (app, base):
        alice = Client(base)
        alice.login()
        project = alice.create_project()
        job = alice.upload(pdf_bytes())[1]
        ready(alice, job["id"])
        app.state.research.sync()
        app.state.investigations.model = FakeModel()
        saved = complete(alice, start(alice)["id"])
    interrupted = uuid4().hex
    with Store(root).connection() as db:
        db.execute(
            "INSERT INTO research_runs (id,project_id,question,mode,status,stage,config,"
            "created_at,updated_at) "
            "SELECT ?,project_id,question,mode,'running','generating',config,created_at,updated_at "
            "FROM research_runs WHERE id=?",
            (interrupted, saved["id"]),
        )
    with running(root) as (_, base):
        alice.base = base
        alice.project = project["id"]
        recovered = alice.request("/api/research/" + interrupted)[1]
        assert recovered["status"] == "failed" and recovered["stage"] == "interrupted"
        persisted = alice.request("/api/research/" + saved["id"])[1]
        assert persisted["status"] == "completed"
        assert persisted["result"]["claims"] == saved["result"]["claims"]


def test_validation_rejects_altered_quotes_and_false_conflicts():
    evidence = [{"id": "E001", "text": "Controller limit is 85 Celsius.", "chunk_start": 12}]
    value = {
        "claims": [
            {
                "text": "limit",
                "subquestion": 0,
                "citations": [{"evidence_id": "E001", "quote": "Controller limit is 90 Celsius."}],
            }
        ],
        "potential_conflicts": [],
        "unresolved": [],
    }
    assert validate_answer(value, evidence, ["limit"])["claims"] == []
    value["claims"][0]["citations"][0]["quote"] = evidence[0]["text"]
    accepted = validate_answer(value, evidence, ["limit"])["claims"][0]["citations"][0]
    assert accepted["chunk_start"] == 12 and accepted["chunk_end"] == 12 + len(evidence[0]["text"])
    value["potential_conflicts"] = [
        {"description": "contradiction", "citations": value["claims"][0]["citations"]}
    ]
    assert validate_answer(value, evidence, ["limit"])["potential_conflicts"] == []


def test_local_model_cancel_kills_request_process():
    class Model(LocalModel):
        pass

    # Cancellation is checked before the child can return any model output.
    def cancelled():
        raise Cancelled("cancelled")

    with pytest.raises(Cancelled):
        Model("http://127.0.0.1:1").generate(
            [{"role": "user", "content": "test"}], {}, 2, cancelled
        )


def test_schema_one_migrates_without_losing_accounts_or_projects(tmp_path):
    store = Store(tmp_path)
    owner = store.add_user("old@example.test", "Original owner", PASSWORD)
    project = store.create_project(owner, "Original project")
    with store.connection() as db:
        db.execute("DROP TABLE research_runs")
        db.execute("PRAGMA user_version=1")
    upgraded = Store(tmp_path)
    assert upgraded.project(owner, project["id"])["name"] == "Original project"
    with upgraded.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM research_runs").fetchone()[0] == 0


def test_source_deletion_cancels_inflight_research_without_revival(research_app):
    app, alice, _, job = research_app
    model = FakeModel("blocked")
    app.state.investigations.model = model
    run = start(alice)
    assert model.entered.wait(10)
    assert alice.request("/api/documents/" + job["id"], method="DELETE")[0] == 200
    assert alice.request("/api/research/" + run["id"])[0] == 404
    time.sleep(0.1)
    assert alice.request("/api/research")[1]["runs"] == []


def test_local_model_stream_protocol_and_pinned_identity():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"models": [{"name": "qwen3:8b", "digest": MODEL_DIGEST}]}).encode()
            )

        def do_POST(self):
            task = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(task)
            self.send_response(200)
            self.end_headers()
            for row in (
                {"message": {"content": '{"test":'}, "done": False},
                {
                    "message": {"content": "true}"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            ):
                self.wfile.write((json.dumps(row) + "\n").encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        result = LocalModel(url).generate(
            [{"role": "user", "content": "test"}], {}, 5, lambda: None
        )
        assert result["output"] == {"test": True}
        assert result["output_tokens"] == 5
        assert requests[0]["stream"] is True and requests[0]["think"] is False
        with pytest.raises(ConnectionError):
            LocalModel(url, digest="wrong").generate(
                [{"role": "user", "content": "test"}], {}, 5, lambda: None
            )
        assert len(requests) == 1
        with pytest.raises(ValueError, match="context budget"):
            LocalModel(url).generate([{"role": "user", "content": "x" * 6001}], {}, 5, lambda: None)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pending_research_limit_applies_across_owned_projects(research_app):
    app, alice, _, _ = research_app
    model = FakeModel("blocked")
    app.state.investigations.model = model
    first = start(alice)
    assert model.entered.wait(10)
    pending = [first, start(alice), start(alice)]
    alice.create_project("Another collection")
    assert alice.request("/api/research", method="POST", body={"question": "controller"})[0] == 429
    for run in pending:
        assert alice.request("/api/research/" + run["id"] + "/cancel", method="POST")[0] == 200


def test_source_selection_attaches_exact_saved_passage():
    source = {
        "id": "E001",
        "text": "The operating limit is 85 Celsius.\nDo not exceed this limit.",
        "chunk_start": 17,
    }
    proposal = {
        "claims": [
            {
                "text": "The limit is 85 Celsius.",
                "subquestion": 0,
                "citations": [{"evidence_id": "E001", "quote": "@E001"}],
            }
        ],
        "unresolved": [],
        "potential_conflicts": [],
    }
    result = validate_answer(proposal, [source], ["What is the limit?"])
    citation = result["claims"][0]["citations"][0]
    assert citation["quote"] == source["text"]
    assert citation["chunk_start"] == 17
    assert citation["chunk_end"] == 17 + len(source["text"])
    proposal["claims"][0]["citations"][0]["quote"] = "@E002"
    assert not validate_answer(proposal, [source], ["What is the limit?"])["claims"]
