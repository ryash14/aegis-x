"""Fusion and pre-ranking scope guarantees with deterministic vectors."""

import json
from types import SimpleNamespace

import pytest

from aegis.app.research import fuse
from aegis.retrieval.dense import DenseIndex
from aegis.retrieval.sparse import SparseIndex


def test_fusion_combines_ranks_and_deduplicates():
    a = {"job_id": "a", "chunk_index": 0}
    b = {"job_id": "b", "chunk_index": 0}
    c = {"job_id": "c", "chunk_index": 0}
    result = fuse([a, b], [b, c])
    assert [hit["job_id"] for hit in result] == ["b", "a", "c"]
    assert result[0]["ranks"] == {"sparse": 2, "dense": 1}


def test_dense_and_sparse_scope_before_top_k(tmp_path):
    np = pytest.importorskip("numpy")
    index = SparseIndex(tmp_path / "index.db")
    for identity in ("private", "other"):
        index.replace(
            {
                "id": identity,
                "identity": identity,
                "signature": "v1",
                "name": identity,
                "format": "pdf",
            },
            [
                {
                    "document_id": identity,
                    "chunk_id": identity,
                    "index": 0,
                    "text": "Controller thermal limit",
                    "kind": "text",
                    "mappings": [],
                    "headings": [{"text": "Operating limits"}],
                }
            ],
        )
    encoder = SimpleNamespace(key="fake", dimension=2, query=lambda _: np.array([1, 0]))
    dense = DenseIndex(index.path, encoder)
    with index.connection() as db:
        for row in db.execute("SELECT rowid FROM retrieval_chunks").fetchall():
            db.execute(
                "INSERT INTO dense_vectors VALUES(?,?,?,?,?)",
                (row[0], "fake", 0, json.dumps({}), np.array([1, 0], dtype="<f4").tobytes()),
            )
    for search in (index.search, dense.search):
        assert search("thermal", job_ids=[], limit=1) == []
        assert (
            search("thermal", job_ids=["private"], section="operating", limit=1)[0]["job_id"]
            == "private"
        )
        assert search("thermal", job_ids=["private"], section="qualification") == []
