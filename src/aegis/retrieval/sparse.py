"""SQLite FTS5 BM25 baseline; literal queries and deterministic result ordering."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

VERSION = "fts5-unicode61-bm25-v1"


class SparseIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as database:
            database.executescript("""
                CREATE TABLE IF NOT EXISTS retrieval_jobs (
                    job_id TEXT PRIMARY KEY, signature TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retrieval_chunks (
                    rowid INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL, chunk_id TEXT NOT NULL,
                    document_id TEXT NOT NULL, name TEXT NOT NULL, format TEXT NOT NULL,
                    kind TEXT NOT NULL, pages TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(job_id,chunk_index)
                );
                CREATE INDEX IF NOT EXISTS retrieval_source ON retrieval_chunks(job_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_text USING fts5(
                    text, tokenize='unicode61 remove_diacritics 2'
                );
            """)

    @contextmanager
    def connection(self):
        database = sqlite3.connect(self.path, timeout=30)
        database.row_factory = sqlite3.Row
        try:
            with database:
                yield database
        finally:
            database.close()

    @staticmethod
    def _remove(database, job_id):
        database.execute(
            "DELETE FROM retrieval_text WHERE rowid IN "
            "(SELECT rowid FROM retrieval_chunks WHERE job_id=?)",
            (job_id,),
        )
        database.execute("DELETE FROM retrieval_chunks WHERE job_id=?", (job_id,))
        database.execute("DELETE FROM retrieval_jobs WHERE job_id=?", (job_id,))

    def replace(self, job, chunks):
        """Atomically replace one source; a bad iterator rolls back the old index."""
        signature = VERSION + ":" + job["signature"]
        with self.connection() as database:
            old = database.execute(
                "SELECT signature FROM retrieval_jobs WHERE job_id=?", (job["id"],)
            ).fetchone()
            if old and old[0] == signature:
                return False
            self._remove(database, job["id"])
            for chunk in chunks:
                if chunk["document_id"] != job["identity"] or not chunk["text"].strip():
                    raise ValueError("Chunk identity or text is invalid")
                pages = sorted(
                    {
                        source["page"]
                        for mapping in chunk["mappings"]
                        for source in mapping["sources"]
                        if source.get("page")
                    }
                )
                cursor = database.execute(
                    "INSERT INTO retrieval_chunks "
                    "(job_id,chunk_index,chunk_id,document_id,name,format,kind,pages,payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        job["id"],
                        chunk["index"],
                        chunk["chunk_id"],
                        chunk["document_id"],
                        job["name"],
                        job["format"],
                        chunk["kind"],
                        json.dumps(pages),
                        json.dumps(chunk, ensure_ascii=False),
                    ),
                )
                database.execute(
                    "INSERT INTO retrieval_text(rowid,text) VALUES (?,?)",
                    (cursor.lastrowid, chunk["text"]),
                )
            database.execute("INSERT INTO retrieval_jobs VALUES (?,?)", (job["id"], signature))
        return True

    def stats(self):
        with self.connection() as database:
            row = database.execute(
                "SELECT count(*),count(DISTINCT job_id) FROM retrieval_chunks"
            ).fetchone()
        return {"chunks": row[0], "documents": row[1], "method": VERSION}

    def sync_workspace(self, workspace):
        """Explicitly index ready snapshots; prune failed/removed sources atomically."""
        with workspace.connection() as database:
            jobs = [
                dict(row)
                for row in database.execute(
                    "SELECT * FROM documents WHERE status='ready' ORDER BY id"
                )
            ]
        current = {job["id"] for job in jobs}
        with self.connection() as database:
            for row in database.execute("SELECT job_id FROM retrieval_jobs").fetchall():
                if row[0] not in current:
                    self._remove(database, row[0])
        updated = sum(
            self.replace(
                job,
                (
                    workspace.read_json(job["id"], f"chunk-{number}.json")
                    for number in range(job["chunks"])
                ),
            )
            for job in jobs
        )
        return {**self.stats(), "updated_documents": updated}

    def search(
        self,
        query,
        *,
        limit=10,
        job_id=None,
        format=None,
        kind=None,
        page=None,
        mode="any",
        live_workspace=False,
    ):
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("Query must contain at most 2,000 characters")
        if type(limit) is not int or not 1 <= limit <= 100 or mode not in {"any", "all"}:
            raise ValueError("Invalid search limit or match mode")
        if page is not None and (type(page) is not int or page < 1):
            raise ValueError("Page must be a positive integer")
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query, flags=re.UNICODE)))
        if len(terms) > 64:
            raise ValueError("Use at most 64 distinct query terms")
        if not terms:
            return []
        # User punctuation and FTS operators are never interpreted as query syntax.
        expression = (" OR " if mode == "any" else " AND ").join(f'"{term}"' for term in terms)
        conditions = ["retrieval_text MATCH ?"]
        parameters = [expression]
        for column, value in (("job_id", job_id), ("format", format), ("kind", kind)):
            if value is not None:
                conditions.append(f"c.{column}=?")
                parameters.append(value)
        if page is not None:
            conditions.append("EXISTS (SELECT 1 FROM json_each(c.pages) WHERE value=?)")
            parameters.append(page)
        if live_workspace:
            conditions.append("c.job_id IN (SELECT id FROM documents WHERE status='ready')")
        parameters.append(limit)
        with self.connection() as database:
            rows = database.execute(
                "SELECT c.*,bm25(retrieval_text) AS score "
                "FROM retrieval_text JOIN retrieval_chunks c ON c.rowid=retrieval_text.rowid "
                "WHERE "
                + " AND ".join(conditions)
                + " ORDER BY score,c.chunk_id,c.job_id,c.chunk_index LIMIT ?",
                parameters,
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result.pop("rowid")
            result["pages"] = json.loads(result["pages"])
            result["chunk"] = json.loads(result.pop("payload"))
            results.append(result)
        return results
