"""Durable, cancellable revision reviews and append-only human decisions."""

import hashlib
import html
import json
import threading
import time
from uuid import uuid4

from .requirements import VERSION, compare, inventory


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def printable(review):
    result = review["result"]

    def escape(value):
        return html.escape(str(value))

    rows = []
    decisions = {item["row_id"]: item for item in review["decisions"]}
    for row in result["rows"]:
        decision = decisions.get(row["id"], {})
        rows.append(
            "<tr>"
            + "".join(
                "<td>" + escape(value) + "</td>"
                for value in (
                    row["id"],
                    row["status"],
                    (row["before"] or {}).get("quote", "—"),
                    (row["after"] or {}).get("quote", "—"),
                    row["quantity_change"],
                    decision.get("decision", "pending"),
                    decision.get("note", ""),
                )
            )
            + "</tr>"
        )
    return "<!doctype html><meta charset='utf-8'><title>AEGIS revision review</title>" + (
        "<style>body{font:14px "
        "system-ui;margin:32px;color:#111}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid "
        "#bbb;padding:8px;text-align:left;vertical-align:top;white-space:pre-wrap}"
        "h1{font-size:24px}@media print{body{margin:0}tr{break-inside:avoid}}</style>"
        "<h1>AEGIS revision review</h1><p>"
        + escape(review["id"])
        + "</p><p>"
        + escape(result["baseline"]["document"]["name"])
        + " → "
        + escape(result["candidate"]["document"]["name"])
        + "</p><p>"
        + escape(" | ".join(result["limitations"]))
        + "</p><p>Coverage: "
        + escape(canonical({side: result[side]["coverage"] for side in ("baseline", "candidate")}))
        + "</p><table><thead><tr><th>ID</th><th>Change</th><th>Baseline</th><th>Candidate</th>"
        "<th>Quantity</th><th>Decision</th><th>Note</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table><h2>Reproduction</h2><pre>"
        + escape(canonical(review["config"]))
        + "</pre><p>Result SHA-256: "
        + escape(review["result_sha256"])
        + "</p><h2>Decision history</h2><pre>"
        + escape(canonical(review["decisions"]))
        + "</pre>"
    )


class Reviews:
    def __init__(self, store, workspace, mutations):
        self.store, self.workspace, self.mutations = store, workspace, mutations
        self.stop = threading.Event()
        with store.connection() as db:
            db.execute(
                "UPDATE reviews SET status='failed',error='Interrupted; retry "
                "explicitly',updated_at=? WHERE status='running'",
                (time.time(),),
            )
        self.thread = threading.Thread(target=self._loop, name="revision-review", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def get(self, owner, identity):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT r.* FROM reviews r JOIN projects p ON p.id=r.project_id WHERE r.id=? "
                "AND p.owner_id=? AND p.status='active'",
                (identity, owner),
            ).fetchone()
            if not row:
                raise KeyError("Review not found")
            value = dict(row)
            value["config"] = json.loads(value["config"])
            value["result"] = json.loads(value["result"]) if value["result"] else None
            value["decisions"] = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM review_decisions WHERE review_id=? ORDER BY sequence",
                    (identity,),
                )
            ]
        return value

    def list(self, owner, project):
        self.store.project(owner, project)
        with self.store.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT "
                    "id,status,baseline_id,candidate_id,created_at,updated_at,progress,error "
                    "FROM reviews WHERE project_id=? ORDER BY created_at DESC LIMIT 50",
                    (project,),
                )
            ]

    def create(self, owner, project, baseline, candidate, *, reuse=True):
        if not isinstance(baseline, str) or not isinstance(candidate, str) or baseline == candidate:
            raise ValueError("Choose two different source documents")
        with self.mutations:
            self.store.project(owner, project)
            documents = []
            for identity in (baseline, candidate):
                metadata = self.store.document(owner, identity, project)
                job = self.workspace.get(identity)
                if job["status"] != "ready":
                    raise ValueError("Both documents must finish processing")
                documents.append(
                    {
                        "id": identity,
                        "name": job["name"],
                        "sha256": job["identity"],
                        "signature": job["signature"],
                        "chunks": job["chunks"],
                        "warnings": self.workspace.read_json(identity, "summary.json").get(
                            "warnings", []
                        ),
                        "revision": metadata["revision"],
                        "role": metadata["role"],
                    }
                )
            config = {
                "version": VERSION,
                "documents": documents,
                "max_chunks_per_document": 2000,
                "timeout_seconds": 120,
            }
            cache_key = hashlib.sha256(canonical(config).encode()).hexdigest()
            now, identity = time.time(), uuid4().hex
            with self.store.connection() as db:
                if reuse:
                    old = db.execute(
                        "SELECT id FROM reviews WHERE project_id=? AND cache_key=? AND "
                        "status='completed' ORDER BY created_at DESC LIMIT 1",
                        (project, cache_key),
                    ).fetchone()
                    if old:
                        return self.get(owner, old[0])
                count = db.execute(
                    "SELECT count(*) FROM reviews r JOIN projects p ON p.id=r.project_id WHERE "
                    "p.owner_id=? AND r.status IN ('queued','running')",
                    (owner,),
                ).fetchone()[0]
                total = db.execute(
                    "SELECT count(*) FROM reviews WHERE status IN ('queued','running')"
                ).fetchone()[0]
                if count >= 3 or total >= 20:
                    raise BlockingIOError("Review queue is full")
                db.execute(
                    "INSERT INTO "
                    "reviews(id,project_id,baseline_id,candidate_id,status,config,"
                    "cache_key,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?,?)",
                    (
                        identity,
                        project,
                        baseline,
                        candidate,
                        canonical(config),
                        cache_key,
                        now,
                        now,
                    ),
                )
            return self.get(owner, identity)

    def cancel(self, owner, identity):
        self.get(owner, identity)
        with self.store.connection() as db:
            db.execute(
                "UPDATE reviews SET status='cancelled',updated_at=? WHERE id=? AND status IN "
                "('queued','running')",
                (time.time(), identity),
            )
        return self.get(owner, identity)

    def decide(self, owner, identity, row_id, decision, note):
        if not isinstance(decision, str) or decision not in {
            "accepted",
            "rejected",
            "needs_followup",
        }:
            raise ValueError("Use accepted, rejected or needs_followup")
        note = self.store.text(note, "Review note", 2000, empty=True, multiline=True)
        with self.mutations:
            review = self.get(owner, identity)
            if review["status"] != "completed" or not any(
                row["id"] == row_id for row in review["result"]["rows"]
            ):
                raise ValueError("Choose a finding from a completed review")
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT entry_hash FROM review_decisions WHERE review_id=? ORDER BY "
                    "sequence DESC LIMIT 1",
                    (identity,),
                ).fetchone()
                entry = {
                    "review_id": identity,
                    "row_id": row_id,
                    "actor_id": owner,
                    "decision": decision,
                    "note": note,
                    "created_at": time.time(),
                    "previous_hash": previous[0] if previous else "",
                }
                digest = hashlib.sha256(canonical(entry).encode()).hexdigest()
                db.execute(
                    "INSERT INTO "
                    "review_decisions(review_id,row_id,actor_id,decision,note,"
                    "created_at,previous_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (*entry.values(), digest),
                )
        return self.get(owner, identity)

    def _loop(self):
        while not self.stop.is_set():
            with self.store.connection() as db:
                row = db.execute(
                    "SELECT r.id,p.owner_id FROM reviews r JOIN projects p ON p.id=r.project_id "
                    "WHERE r.status='queued' AND p.status='active' ORDER BY r.created_at LIMIT 1"
                ).fetchone()
            if row:
                self.execute(row["owner_id"], row["id"])
            else:
                self.stop.wait(0.3)

    def execute(self, owner, identity):
        started = time.monotonic()

        def check():
            review = self.get(owner, identity)
            with self.store.connection() as db:
                active = db.execute("SELECT active FROM users WHERE id=?", (owner,)).fetchone()
            if (
                not active
                or not active[0]
                or self.stop.is_set()
                or review["status"] not in {"queued", "running"}
            ):
                raise InterruptedError("Review stopped")
            if time.monotonic() - started > 120:
                raise TimeoutError("Review exceeded its time budget")
            return review

        try:
            review = check()
            with self.store.connection() as db:
                db.execute(
                    "UPDATE reviews SET status='running',updated_at=? WHERE id=? AND "
                    "status='queued'",
                    (time.time(), identity),
                )
            inventories = []
            for document in review["config"]["documents"]:

                def chunks(document=document):
                    for number in range(
                        min(document["chunks"], review["config"]["max_chunks_per_document"])
                    ):
                        check()
                        with self.mutations:
                            self.store.document(owner, document["id"], review["project_id"])
                            chunk = self.workspace.read_json(document["id"], f"chunk-{number}.json")
                        if number % 20 == 0:
                            with self.store.connection() as db:
                                db.execute(
                                    "UPDATE reviews SET progress=?,updated_at=? WHERE id=? AND "
                                    "status='running'",
                                    (number, time.time(), identity),
                                )
                        yield chunk

                inventories.append(inventory(chunks(), document))
            check()
            result = compare(*inventories, check=check)
            encoded = canonical(result)
            if len(encoded.encode()) > 32 * 1024**2:
                raise ValueError("Review report exceeds 32 MiB; split the documents")
            with self.mutations:
                check()
                for document in review["config"]["documents"]:
                    self.store.document(owner, document["id"], review["project_id"])
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE reviews SET "
                        "status='completed',result=?,result_sha256=?,updated_at=? WHERE id=? "
                        "AND status='running'",
                        (
                            encoded,
                            hashlib.sha256(encoded.encode()).hexdigest(),
                            time.time(),
                            identity,
                        ),
                    )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, (ValueError, TimeoutError, InterruptedError))
                else "Review failed; check source availability and retry"
            )
            with self.store.connection() as db:
                db.execute(
                    "UPDATE reviews SET status='failed',error=?,updated_at=? WHERE id=? AND "
                    "status IN ('queued','running')",
                    (message, time.time(), identity),
                )
