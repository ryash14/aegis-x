"""Exact vector ranking, window aggregation, persistence and invalidation."""

import pytest

from aegis.retrieval.dense import DenseIndex
from aegis.retrieval.sparse import SparseIndex

np = pytest.importorskip("numpy")


class Encoder:
    key = "test-model-v1"
    dimension = 2

    def passages(self, text):
        values = {"pressure": [[1, 0]], "temperature": [[0, 1]], "long": [[0, 1], [1, 0]]}[text]
        return (
            [
                {"token_start": n, "token_end": n + 1, "char_start": 0, "char_end": len(text)}
                for n in range(len(values))
            ],
            np.array(values, dtype="<f4"),
        )

    def query(self, text):
        return np.array([1, 0], dtype="<f4")


def put(index, name, text, signature="v1", page=1):
    index.replace(
        {
            "id": name,
            "signature": signature,
            "identity": name,
            "name": name + ".pdf",
            "format": "pdf",
        },
        [
            {
                "document_id": name,
                "index": 0,
                "chunk_id": name,
                "text": text,
                "kind": "prose",
                "mappings": [{"sources": [{"page": page}]}],
            }
        ],
    )


def test_cosine_windows_filters_restart_and_invalidation(tmp_path):
    path = tmp_path / "index.db"
    sparse = SparseIndex(path)
    put(sparse, "a", "pressure", page=2)
    put(sparse, "b", "temperature")
    put(sparse, "c", "long")
    dense = DenseIndex(path, Encoder())
    assert dense.sync()["windows"] == 4
    assert dense.sync()["encoded_chunks"] == 0
    results = dense.search("force per area")
    assert [hit["chunk_id"] for hit in results] == ["a", "c", "b"]
    assert results[1]["window"]["token_start"] == 1
    assert DenseIndex(path, Encoder()).search("anything") == results
    assert dense.search("query", page=2)[0]["chunk_id"] == "a"
    assert not dense.search("query", format="docx")
    assert dense.search("query", job_id="b")[0]["score"] == 0
    assert not dense.search("")
    put(sparse, "a", "temperature", signature="v2")
    assert dense.stats()["chunks"] == 2  # Foreign-key deletion invalidates old vectors.
    dense.sync()
    assert dense.search("query")[0]["chunk_id"] == "c"


def test_model_identity_and_validation(tmp_path):
    path = tmp_path / "index.db"
    sparse = SparseIndex(path)
    put(sparse, "a", "pressure")
    dense = DenseIndex(path, Encoder())
    dense.sync()
    different = Encoder()
    different.key = "different-model"
    assert DenseIndex(path, different).search("query") == []
    for options in ({"limit": 0}, {"limit": True}, {"page": -1}, {"mode": "raw"}):
        with pytest.raises(ValueError):
            dense.search("query", **options)
    with pytest.raises(ValueError):
        dense.search("x" * 2001)


def test_token_windows_cover_oversized_text_and_query_rejects_truncation():
    from types import SimpleNamespace

    from aegis.retrieval.embeddings import LocalEncoder

    class Tokenizer:
        def encode(self, text, add_special_tokens):
            return SimpleNamespace(
                ids=list(range(len(text))), offsets=[(n, n + 1) for n in range(len(text))]
            )

    encoder = LocalEncoder.__new__(LocalEncoder)
    encoder.tokenizer = Tokenizer()
    encoder.cls, encoder.sep, encoder.np = 101, 102, np
    sequences = []

    def run(batch):
        sequences.extend(batch)
        return np.zeros((len(batch), 384), dtype="<f4")

    encoder._run = run
    text = "x" * 1300
    windows, vectors = encoder.passages(text)
    assert vectors.shape == (3, 384)
    assert windows[0]["token_start"] == 0 and windows[-1]["token_end"] == len(text)
    assert all(len(sequence) <= 512 for sequence in sequences)
    assert all(
        left["token_end"] - right["token_start"] == 64
        for left, right in zip(windows, windows[1:], strict=False)
    )
    assert windows[-1]["char_end"] == len(text)
    with pytest.raises(ValueError, match="budget"):
        encoder.query("x" * 510)


def test_local_model_manifest_rejects_unapproved_revision(tmp_path):
    import json

    from aegis.retrieval.embeddings import LocalEncoder

    (tmp_path / "manifest.json").write_text(json.dumps({"model": "other", "revision": "v1"}))
    with pytest.raises(ValueError, match="revision"):
        LocalEncoder(tmp_path)
