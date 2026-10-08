"""Source identity, conservative numerical checks and private review lifecycle."""

import json

import pymupdf
import pytest

from aegis.app.requirements import compare, constraint, inventory
from aegis.app.research import topic_match

from .test_private_app import private_app, ready, until  # noqa: F401


def source(text, identity="old"):
    chunk = {"index": 0, "chunk_id": "chunk", "text": text, "mappings": [], "headings": []}
    document = {"id": identity, "sha256": identity, "name": identity, "chunks": 1}
    return inventory([chunk], document)


def pdf(text):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox((40, 40, 550, 750), text, fontsize=11)
        return document.tobytes()


def test_inventory_preserves_original_ranges_and_modalities():
    text = (
        "REQ-THERM-001: Controller maximum operating temperature shall be 85 Celsius.\n\n"
        "The author describes a test."
    )
    result = source(text)
    assert len(result["requirements"]) == 1
    item = result["requirements"][0]
    assert text[item["start"] : item["end"]] == item["quote"]
    assert item["explicit_id"] == "REQ-THERM-001"
    assert item["constraint"]["upper"] == 85
    assert result["coverage"]["exhaustive"] is False


@pytest.mark.parametrize(
    "text",
    [
        "Velocity shall be 10 m/s.",
        "Voltage shall be 5 V or 12 V.",
        "The value shall be 10 widgets.",
        "Power shall be 3 W ± 1 W.",
        "Voltage shall be at least 10 and 20 V.",
        "Voltage shall be 5 MV.",
        "Conductance shall be 5 S.",
        "Charge shall be 5 C.",
        "Temperature shall not be below 80 Celsius.",
    ],
)
def test_unsupported_quantities_are_manual(text):
    assert constraint(text) is None


def test_revision_changes_equivalence_conditions_and_duplicate_ids():
    a = source("REQ-001: Controller maximum temperature shall be 85 Celsius.")
    b = source("REQ-001: Controller maximum temperature shall be 80 Celsius.", "new")
    assert compare(a, b)["rows"][0]["quantity_change"] == "tightened"
    equivalent = source("REQ-001: Controller maximum temperature shall be 358.15 Kelvin.", "new")
    assert compare(a, equivalent)["rows"][0]["quantity_change"] == "equivalent_quantity"
    conditional = source(
        "REQ-001: Controller maximum temperature shall be 80 Celsius when powered down.", "new"
    )
    assert compare(a, conditional)["rows"][0]["quantity_change"] == "condition_changed"
    duplicate = source(
        "REQ-001: Controller maximum temperature shall be 80 Celsius.\n\n"
        "REQ-001: Controller maximum temperature shall be 70 Celsius.",
        "new",
    )
    assert compare(a, duplicate)["rows"][0]["status"] == "ambiguous"
    assert compare(a, source("No modal requirement here.", "new"))["counts"] == {"removed": 1}


def test_topic_typo_prefers_compound_subject_over_generic_ingestion():
    topic = {
        "text": "Long-term context injection. We combine local attention and retrieved memory.",
        "headings": [],
    }
    generic = {"text": "Document ingestion. This extracts PDF pages into text.", "headings": []}
    for question in [
        "Long term content ingestion",
        "Long term context ingestion",
        "Long term congestion ingestion",
    ]:
        assert topic_match(question, topic)[0] > topic_match(question, generic)[0]
        assert topic_match(question, topic)[0] >= 0.68


def test_private_review_decision_export_cache_and_source_deletion(private_app):  # noqa: F811
    app, alice, bob = private_app
    alice.create_project()
    a = alice.upload(
        pdf("REQ-THERM-001: Controller maximum operating temperature shall be 85 Celsius."),
        "baseline.pdf",
    )[1]
    b = alice.upload(
        pdf(
            "REQ-THERM-001: Controller maximum operating temperature shall be 80 Celsius.\n\n"
            "REQ-POWER-002: Power shall be at most 5 W."
        ),
        "candidate.pdf",
    )[1]
    for job in [a, b]:
        ready(alice, job["id"])
    status, review, _ = alice.request(
        "/api/reviews", method="POST", body={"baseline_id": a["id"], "candidate_id": b["id"]}
    )
    assert status == 202, review
    identity = review["id"]
    until(lambda: alice.request("/api/reviews/" + identity)[1]["status"] in {"completed", "failed"})
    review = alice.request("/api/reviews/" + identity)[1]
    assert review["status"] == "completed", review
    assert review["result"]["counts"] == {"changed": 1, "added": 1}
    assert review["result"]["rows"][0]["quantity_change"] == "tightened"
    assert bob.request("/api/reviews/" + identity)[0] == 404
    assert bob.request("/api/reviews/" + identity + "/report")[0] == 404
    assert (
        alice.request(
            "/api/reviews/" + identity + "/decisions",
            method="POST",
            body={"row_id": "R0001", "decision": [], "note": ""},
        )[0]
        == 400
    )
    for decision in ["accepted", "needs_followup"]:
        status, review, _ = alice.request(
            "/api/reviews/" + identity + "/decisions",
            method="POST",
            body={"row_id": "R0001", "decision": decision, "note": "<script>alert(1)</script>"},
        )
        assert status == 200, review
    assert len(review["decisions"]) == 2
    assert review["decisions"][1]["previous_hash"] == review["decisions"][0]["entry_hash"]
    exported = alice.request("/api/reviews/" + identity + "/report?format=html")
    assert (
        exported[0] == 200 and b"&lt;script&gt;" in exported[1] and b"<script>" not in exported[1]
    )
    assert json.loads(json.dumps(review))["result_sha256"]
    from .test_investigation import FakeModel

    app.state.investigations.model = FakeModel()
    linked_body = {
        "question": "Explain the controller thermal limit change.",
        "retrieval": "sparse",
        "review_id": identity,
        "review_row_id": "R0001",
    }
    status, linked, _ = alice.request("/api/research", method="POST", body=linked_body)
    assert status == 202, linked
    assert linked["config"]["review_reference"]["result_sha256"] == review["result_sha256"]
    assert bob.request("/api/research", method="POST", body=linked_body)[0] in {400, 404}
    linked_body["review_row_id"] = "R9999"
    assert alice.request("/api/research", method="POST", body=linked_body)[0] == 400
    again = alice.request(
        "/api/reviews", method="POST", body={"baseline_id": a["id"], "candidate_id": b["id"]}
    )[1]
    assert again["id"] == identity
    assert alice.request("/api/documents/" + a["id"], method="DELETE")[0] == 200
    assert alice.request("/api/reviews/" + identity)[0] == 404


def test_same_id_different_subject_or_modality_cannot_imply_quantity_equivalence():
    a = source("REQ-001: Controller maximum temperature shall be 85 Celsius.")
    for quote in [
        "REQ-001: Battery maximum temperature shall be 85 Celsius.",
        "REQ-001: Controller maximum temperature should be 85 Celsius.",
    ]:
        assert compare(a, source(quote, "new"))["rows"][0]["quantity_change"] == "manual"


def test_inventory_cap_applies_within_a_single_chunk():
    text = "\n\n".join(f"REQ-{n:04}: Voltage shall be {n} V." for n in range(1100))
    result = source(text)
    assert len(result["requirements"]) == 1000
    assert result["coverage"]["warnings"]
