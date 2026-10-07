"""Incremental local indexes and authorized, bounded evidence retrieval."""

import json
import logging
import re
import threading
import time

from aegis.retrieval.dense import DenseIndex
from aegis.retrieval.embeddings import LocalEncoder
from aegis.retrieval.sparse import SparseIndex

LOG = logging.getLogger(__name__)


STOPWORDS = frozenset(
    "a an the is are was were be been being do does did how what which where when why "
    "who to of for on in at by with from and or as it its we our you your each this that "
    "these those can could should would will must much some any all into have has had".split()
)


def lexical_query(query):
    """Remove English function words, retaining technical identifiers and quantities."""
    terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    meaningful = [term for term in terms if term.lower() not in STOPWORDS]
    return " ".join(meaningful) if meaningful else query


def fuse(sparse, dense, limit=10):
    """Reciprocal rank fusion; unlike raw scores, rankings share a common scale."""
    scores, hits, ranks = {}, {}, {}
    for method, candidates in (("sparse", sparse), ("dense", dense)):
        for rank, hit in enumerate(candidates, 1):
            key = (hit["job_id"], hit["chunk_index"])
            hits.setdefault(key, dict(hit))
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            ranks.setdefault(key, {})[method] = rank
    return [
        {**hits[key], "score": scores[key], "method": "hybrid", "ranks": ranks[key]}
        for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    ]


class Research:
    def __init__(self, workspace, store, mutations, model_directory, *, encoder=None):
        self.workspace, self.store, self.mutations = workspace, store, mutations
        self.sparse = SparseIndex(workspace.db)
        self.dense = None
        self.model_error = None
        try:
            self.dense = DenseIndex(workspace.db, encoder or LocalEncoder(model_directory))
        except (ImportError, OSError, ValueError):
            self.model_error = (
                "Local embeddings unavailable; install the dense extra and verified model"
            )
            LOG.warning(self.model_error)
        self.stop = threading.Event()
        self.index_lock = threading.Lock()
        self.query_slots = threading.BoundedSemaphore(2)
        self.index_error = False
        self.thread = threading.Thread(target=self._run, name="evidence-indexer", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.sync()
                self.index_error = False
            except Exception:
                self.index_error = True
                LOG.exception("Evidence indexing failed")
            self.stop.wait(2)

    def sync(self):
        with self.index_lock:
            with self.workspace.connection() as db:
                jobs = [
                    dict(row) for row in db.execute("SELECT * FROM documents WHERE status='ready'")
                ]
            for job in jobs:
                if self.stop.is_set():
                    return
                # Serialize each source replacement against deletion, not all embedding work.
                with self.mutations:
                    try:
                        self.workspace.get(job["id"])
                    except KeyError:
                        continue
                    self.sparse.replace(
                        job,
                        (
                            self.workspace.read_json(job["id"], f"chunk-{number}.json")
                            for number in range(job["chunks"])
                        ),
                    )
            if self.dense:
                self.dense.sync(stop=self.stop)

    def scope(self, owner, project, **filters):
        self.store.project(owner, project)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM project_documents WHERE project_id=?", (project,)
            ).fetchall()
        documents = {row["document_id"]: dict(row) for row in rows}
        for field in ("revision", "role"):
            value = filters.get(field)
            if value:
                documents = {key: row for key, row in documents.items() if row[field] == value}
        if filters.get("document_id"):
            identity = filters["document_id"]
            if identity not in documents:
                raise KeyError(identity)
            documents = {identity: documents[identity]}
        return documents

    def status(self, owner, project):
        with self.mutations:
            documents = self.scope(owner, project)
            with self.sparse.connection() as db:
                rows = (
                    db.execute(
                        "SELECT c.job_id,count(*) AS chunks,count(d.source_row) AS embedded "
                        "FROM retrieval_chunks c LEFT JOIN "
                        "(SELECT DISTINCT source_row FROM dense_vectors WHERE model_key=?) d "
                        "ON c.rowid=d.source_row WHERE c.job_id IN "
                        "(SELECT value FROM json_each(?)) "
                        "GROUP BY c.job_id",
                        (self.dense.encoder.key if self.dense else "", json.dumps(list(documents))),
                    ).fetchall()
                    if self.dense
                    else db.execute(
                        "SELECT job_id,count(*) AS chunks,0 AS embedded FROM retrieval_chunks "
                        "WHERE job_id IN (SELECT value FROM json_each(?)) GROUP BY job_id",
                        (json.dumps(list(documents)),),
                    ).fetchall()
                )
            with self.workspace.connection() as db:
                sources = [
                    dict(row)
                    for row in db.execute(
                        "SELECT id,name,status FROM documents WHERE id IN "
                        "(SELECT value FROM json_each(?)) ORDER BY name,id",
                        (json.dumps(list(documents)),),
                    )
                ]
            return {
                "sources": sources,
                "documents": [dict(row) for row in rows],
                "dense_available": self.dense is not None,
                "model_error": self.model_error,
                "index_error": self.index_error,
                "model_key": self.dense.encoder.key if self.dense else None,
            }

    def overview(self, owner, project, *, document_id=None):
        """Use opening passages for broad summaries instead of ranking the word document."""
        hits = []
        with self.mutations:
            documents = self.scope(owner, project, document_id=document_id)
            for identity, metadata in documents.items():
                job = self.workspace.get(identity)
                if job["status"] != "ready":
                    continue
                for number in range(min(job["chunks"], 4)):
                    chunk = self.workspace.read_json(identity, f"chunk-{number}.json")
                    pages = sorted(
                        {
                            mapping["page"]
                            for mapping in chunk["mappings"]
                            if mapping.get("page") is not None
                        }
                    )
                    hits.append(
                        {
                            "job_id": identity,
                            "chunk_index": number,
                            "chunk": chunk,
                            "name": job["name"],
                            "revision": metadata["revision"],
                            "role": metadata["role"],
                            "pages": pages,
                            "source_url": f"/?document={identity}&chunk={number}",
                        }
                    )
                if len(hits) >= 20:
                    break
        return hits[:20]

    def search(self, owner, project, query, *, mode="hybrid", limit=10, budget=12000, **filters):
        if mode not in {"sparse", "dense", "hybrid"}:
            raise ValueError("Use sparse, dense or hybrid retrieval")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Enter a query of 1–2,000 characters")
        if not 1 <= limit <= 20 or not 1000 <= budget <= 24000:
            raise ValueError("Invalid result or context limit")
        if mode != "sparse" and not self.dense:
            raise RuntimeError(self.model_error)
        if not self.query_slots.acquire(blocking=False):
            raise BlockingIOError("Two evidence searches are already running")
        started = time.perf_counter()
        try:
            with self.mutations:
                documents = self.scope(owner, project, **filters)
            options = dict(
                job_ids=list(documents),
                live_workspace=True,
                limit=100,
                section=filters.get("section"),
                kind=filters.get("kind"),
                format=filters.get("format"),
                page=filters.get("page"),
            )
            sparse = self.sparse.search(lexical_query(query), **options) if mode != "dense" else []
            dense = self.dense.search(query, **options) if mode != "sparse" else []
            ranked = fuse(sparse, dense, 100) if mode == "hybrid" else sparse or dense
            hits, seen, used = [], set(), 0
            with self.mutations:
                live = self.scope(owner, project, **filters)
                for hit in ranked:
                    if len(hits) >= limit:
                        break
                    identity = hit["job_id"]
                    if identity not in live:
                        continue
                    try:
                        job = self.workspace.get(identity)
                    except KeyError:
                        continue
                    if job["status"] != "ready":
                        continue
                    signature = (job["identity"], hit["chunk"]["text"])
                    if signature in seen:
                        continue
                    seen.add(signature)
                    text = hit["chunk"]["text"]
                    if used + len(text) > budget:
                        continue
                    used += len(text)
                    hit.update(
                        revision=live[identity]["revision"],
                        role=live[identity]["role"],
                        neighbors=[],
                        source_url=f"/?document={identity}&chunk={hit['chunk_index']}"
                        + (f"&page={hit['pages'][0]}" if hit["pages"] else ""),
                    )
                    hits.append(hit)
                # Reserve the budget for ranked anchors first, then add unique neighbours.
                included = {(hit["job_id"], hit["chunk_index"]) for hit in hits}
                for hit in hits:
                    identity = hit["job_id"]
                    job = self.workspace.get(identity)
                    for number in (hit["chunk_index"] - 1, hit["chunk_index"] + 1):
                        if not 0 <= number < job["chunks"] or (identity, number) in included:
                            continue
                        chunk = self.workspace.read_json(identity, f"chunk-{number}.json")
                        signature = (job["identity"], chunk["text"])
                        if (
                            signature not in seen
                            and chunk.get("headings") == hit["chunk"].get("headings")
                            and chunk["kind"] == hit["chunk"]["kind"]
                            and used + len(chunk["text"]) <= budget
                        ):
                            hit["neighbors"].append(chunk)
                            included.add((identity, number))
                            seen.add(signature)
                            used += len(chunk["text"])
            return {
                "results": hits,
                "mode": mode,
                "context_chars": used,
                "context_budget": budget,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "model_key": self.dense.encoder.key if self.dense and mode != "sparse" else None,
            }
        finally:
            self.query_slots.release()
