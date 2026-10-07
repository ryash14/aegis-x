"""Independent ranking, transactions, literal queries and source-filter checks."""

import pytest

from aegis.retrieval import SparseIndex


def job(identity, signature="v1"):
    return {
        "id": identity,
        "signature": signature,
        "identity": identity,
        "name": identity + ".pdf",
        "format": "pdf",
    }


def chunk(identity, index, text, page=1, kind="prose"):
    return {
        "document_id": identity,
        "index": index,
        "chunk_id": identity + str(index),
        "text": text,
        "kind": kind,
        "mappings": [{"sources": [{"page": page, "start": 0, "end": len(text)}]}],
    }


def test_ranking_filters_and_persistence(tmp_path):
    path = tmp_path / "index.db"
    index = SparseIndex(path)
    index.replace(
        job("a"),
        [
            chunk("a", 0, "rocket pressure pressure", 2),
            chunk("a", 1, "rocket temperature stable", 3, "table"),
        ],
    )
    index.replace(job("b"), [chunk("b", 0, "pressure " + "unrelated " * 100)])
    results = index.search("pressure")
    assert results[0]["chunk_id"] == "a0"
    assert results[0]["chunk"]["mappings"][0]["sources"][0]["page"] == 2
    assert index.search("rocket pressure", mode="all")[0]["chunk_id"] == "a0"
    assert not index.search("pressure", job_id="a", page=3)
    assert not index.search("pressure", format="docx")
    assert index.search("rocket", kind="table")[0]["chunk_id"] == "a1"
    assert SparseIndex(path).search("pressure") == results
    assert index.stats()["chunks"] == 3
    assert not index.replace(job("a"), [])  # Unchanged immutable snapshot is reused.


def test_atomic_replace_rollback_and_no_fts_operators(tmp_path):
    index = SparseIndex(tmp_path / "index.db")
    index.replace(job("a"), [chunk("a", 0, "rocket AND pressure café")])
    assert index.search('"rocket" OR * : -')
    assert index.search("cafe")
    assert index.search("🚀?!") == []
    assert not index.search("rocket absent", mode="all")
    with pytest.raises(ValueError):
        index.replace(job("a", "v2"), [chunk("wrong", 0, "replacement")])
    assert index.search("pressure")
    index.replace(job("a", "v2"), [chunk("a", 0, "replacement")])
    assert not index.search("pressure")
    assert index.stats()["chunks"] == 1


@pytest.mark.parametrize(
    "options", [{"limit": 0}, {"limit": True}, {"page": -1}, {"mode": "raw"}, {"limit": 101}]
)
def test_invalid_options(tmp_path, options):
    with pytest.raises(ValueError):
        SparseIndex(tmp_path / "index.db").search("rocket", **options)


def test_invalid_and_bounded_query(tmp_path):
    index = SparseIndex(tmp_path / "index.db")
    for query in (None, "x" * 2001, " ".join(f"term{i}" for i in range(65))):
        with pytest.raises(ValueError):
            index.search(query)
