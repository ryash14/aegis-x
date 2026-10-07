"""Controlled paraphrase smoke evaluation; not a held-out domain benchmark."""

import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from aegis.app.research import fuse, lexical_query
from aegis.retrieval.dense import DenseIndex
from aegis.retrieval.embeddings import LocalEncoder
from aegis.retrieval.sparse import SparseIndex

ROOT = Path(__file__).resolve().parents[1]
# Human-authored relevance labels fixed before querying the model.
CASES = [
    (
        "pressure",
        "The chamber pressure must remain below forty-two kilopascals.",
        "How much force is exerted on each unit of surface area?",
    ),
    (
        "thermal",
        "Temperature sensors monitor overheating in the propulsion assembly.",
        "How do we detect excessive heat in the engine?",
    ),
    (
        "validation",
        "Product validation confirms the system fulfils stakeholder expectations "
        "in its intended operating environment.",
        "Are we building what the customer actually needs?",
    ),
    (
        "verification",
        "Product verification demonstrates compliance with specified requirements "
        "using inspection, analysis, testing or demonstration.",
        "How do we prove the design satisfies each required specification?",
    ),
    (
        "configuration",
        "Configuration management controls changes to an approved design baseline "
        "and tracks version history.",
        "How are modifications to an agreed design kept under control?",
    ),
    (
        "risk",
        "Risk assessment considers the probability of failure and severity of its consequences.",
        "How should we judge the chance and impact of things going wrong?",
    ),
    (
        "interface",
        "Interface definitions describe connections and data exchanged between subsystems.",
        "Where do we specify how separate components communicate?",
    ),
    (
        "schedule",
        "The project schedule records activity durations, dependencies and completion dates.",
        "How do we plan when dependent tasks must finish?",
    ),
]


def main():
    encoder = LocalEncoder(ROOT / "data/models/bge-small-en-v1.5")
    with tempfile.TemporaryDirectory(prefix="aegis-dense-eval-") as directory:
        sparse = SparseIndex(Path(directory) / "index.db")
        for identity, text, _ in CASES:
            sparse.replace(
                {
                    "id": identity,
                    "signature": "curated-v1",
                    "identity": identity,
                    "name": identity,
                    "format": "pdf",
                },
                [
                    {
                        "document_id": identity,
                        "chunk_id": identity,
                        "index": 0,
                        "text": text,
                        "kind": "prose",
                        "mappings": [{"sources": [{"page": 1}]}],
                    }
                ],
            )
        dense = DenseIndex(sparse.path, encoder)
        indexing = dense.sync()
        results = []
        for expected, _, query in CASES:
            row = {"query": query, "expected": expected}
            rankings = {}
            for name, index in (("sparse", sparse), ("dense", dense)):
                started = time.perf_counter()
                hits = index.search(lexical_query(query) if name == "sparse" else query, limit=8)
                rankings[name] = hits
                order = [hit["chunk_id"] for hit in hits]
                row[name] = {
                    "rank": order.index(expected) + 1 if expected in order else None,
                    "top3": order[:3],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            started = time.perf_counter()
            order = [hit["chunk_id"] for hit in fuse(rankings["sparse"], rankings["dense"], 8)]
            row["hybrid"] = {
                "rank": order.index(expected) + 1 if expected in order else None,
                "top3": order[:3],
                "latency_ms": round(
                    row["sparse"]["latency_ms"]
                    + row["dense"]["latency_ms"]
                    + (time.perf_counter() - started) * 1000,
                    3,
                ),
            }
            results.append(row)
    metrics = {}
    for name in ("sparse", "dense", "hybrid"):
        ranks = [row[name]["rank"] for row in results]
        metrics[name] = {
            "recall_at_1": sum(rank == 1 for rank in ranks) / len(ranks),
            "recall_at_3": sum(rank is not None and rank <= 3 for rank in ranks) / len(ranks),
            "mrr": sum(1 / rank for rank in ranks if rank) / len(ranks),
        }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "model_key": encoder.key,
        "scope": "8 controlled English paraphrases; development smoke set, not held-out",
        "indexing": indexing,
        "metrics": metrics,
        "latency_mean_ms": {
            name: round(sum(row[name]["latency_ms"] for row in results) / len(results), 2)
            for name in metrics
        },
        "queries": results,
    }
    target = ROOT / "data/evaluation/retrieval.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
