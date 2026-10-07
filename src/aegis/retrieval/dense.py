"""Persistent exact cosine search, bounded vector scans and source-preserving results."""

import heapq
import json
import time

from .sparse import SparseIndex


class DenseIndex:
    def __init__(self, path, encoder):
        self.sparse = SparseIndex(path)
        self.encoder = encoder
        with self.sparse.connection() as database:
            database.execute("""CREATE TABLE IF NOT EXISTS dense_vectors (
                source_row INTEGER NOT NULL REFERENCES retrieval_chunks(rowid) ON DELETE CASCADE,
                model_key TEXT NOT NULL, window INTEGER NOT NULL,
                provenance TEXT NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY(source_row,model_key,window)
            )""")

    def stats(self):
        with self.sparse.connection() as database:
            row = database.execute(
                "SELECT count(*),count(DISTINCT source_row) FROM dense_vectors WHERE model_key=?",
                (self.encoder.key,),
            ).fetchone()
        return {
            "windows": row[0],
            "chunks": row[1],
            "dimension": self.encoder.dimension,
            "model_key": self.encoder.key,
            "method": "dense-cosine-exact-v1",
        }

    def sync(self, progress=None, stop=None):
        import numpy as np

        started = time.monotonic()
        encoded = 0
        for row in self.pending():
            if stop and stop.is_set():
                break
            windows, vectors = self.encoder.passages(json.loads(row["payload"])["text"])
            if not windows or len(windows) != len(vectors):
                raise ValueError("Embedding window count mismatch")
            with self.sparse.connection() as database:
                database.execute("BEGIN IMMEDIATE")
                current = database.execute(
                    "SELECT payload FROM retrieval_chunks WHERE rowid=?", (row["rowid"],)
                ).fetchone()
                if not current or current[0] != row["payload"]:
                    continue
                for position, (window, vector) in enumerate(zip(windows, vectors, strict=True)):
                    if (
                        vector.shape != (self.encoder.dimension,)
                        or not np.isfinite(vector).all()
                        or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4)
                    ):
                        raise ValueError(
                            "Embedding vector must be finite, normalized and dimension-correct"
                        )
                    database.execute(
                        "INSERT OR REPLACE INTO dense_vectors VALUES (?,?,?,?,?)",
                        (
                            row["rowid"],
                            self.encoder.key,
                            position,
                            json.dumps(window),
                            vector.astype("<f4").tobytes(),
                        ),
                    )
            encoded += 1
            if progress:
                progress(encoded)
        return {
            **self.stats(),
            "encoded_chunks": encoded,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def pending(self):
        """Close read transactions before inference/writes; fetch bounded batches."""
        with self.sparse.connection() as database:
            ceiling = database.execute(
                "SELECT coalesce(max(rowid),0) FROM retrieval_chunks"
            ).fetchone()[0]
        cursor = 0
        while cursor < ceiling:
            with self.sparse.connection() as database:
                rows = database.execute(
                    "SELECT c.rowid,c.payload FROM retrieval_chunks c WHERE c.rowid>? "
                    "AND c.rowid<=? AND NOT EXISTS (SELECT 1 FROM dense_vectors d "
                    "WHERE d.source_row=c.rowid AND d.model_key=?) ORDER BY c.rowid LIMIT 128",
                    (cursor, ceiling, self.encoder.key),
                ).fetchall()
            if not rows:
                break
            cursor = rows[-1]["rowid"]
            yield from rows

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
        job_ids=None,
        section=None,
    ):
        import numpy as np

        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("Query must contain at most 2,000 characters")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid result limit")
        if page is not None and (type(page) is not int or page < 1):
            raise ValueError("Page must be a positive integer")
        if mode not in {"any", "all"}:
            raise ValueError("Invalid match mode")
        if not query.strip():
            return []
        vector = self.encoder.query(query)
        conditions = ["d.model_key=?"]
        parameters = [self.encoder.key]
        for column, value in (("job_id", job_id), ("format", format), ("kind", kind)):
            if value is not None:
                conditions.append(f"c.{column}=?")
                parameters.append(value)
        if page is not None:
            conditions.append("EXISTS (SELECT 1 FROM json_each(c.pages) WHERE value=?)")
            parameters.append(page)
        if job_ids is not None:
            conditions.append("c.job_id IN (SELECT value FROM json_each(?))")
            parameters.append(json.dumps(list(job_ids)))
        if section:
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(c.payload,'$.headings') "
                "WHERE instr(lower(json_extract(value,'$.text')),lower(?))>0)"
            )
            parameters.append(section)
        if live_workspace:
            conditions.append("c.job_id IN (SELECT id FROM documents WHERE status='ready')")
        winners = []
        with self.sparse.connection() as database:
            rows = database.execute(
                "SELECT c.rowid,c.chunk_id,c.job_id,d.vector,d.provenance "
                "FROM retrieval_chunks c JOIN dense_vectors d ON d.source_row=c.rowid WHERE "
                + " AND ".join(conditions)
                + " ORDER BY c.rowid,d.window",
                parameters,
            )
            current = None
            best = None
            for row in rows:
                candidate = np.frombuffer(row["vector"], dtype="<f4")
                if candidate.shape != (self.encoder.dimension,) or not np.isfinite(candidate).all():
                    raise ValueError("Invalid stored vector")
                score = float(candidate @ vector)
                if row["rowid"] != current and best is not None:
                    winners.append(best)
                    winners = heapq.nsmallest(limit, winners)
                    best = None
                current = row["rowid"]
                result = (-score, row["chunk_id"], row["job_id"], current, row["provenance"])
                if best is None or result < best:
                    best = result
            if best is not None:
                winners.append(best)
            results = []
            for negative, chunk_id, job_id, identity, provenance in heapq.nsmallest(limit, winners):
                row = database.execute(
                    "SELECT * FROM retrieval_chunks WHERE rowid=?", (identity,)
                ).fetchone()
                if not row or (row["chunk_id"], row["job_id"]) != (chunk_id, job_id):
                    continue
                result = dict(row)
                result.pop("rowid")
                result["pages"] = json.loads(result["pages"])
                result["chunk"] = json.loads(result.pop("payload"))
                result.update(score=-negative, method="dense", window=json.loads(provenance))
                results.append(result)
        return results
