"""Deterministic, source-preserving requirement inventory and revision comparison.

Findings are review proposals. Unknown units, conditions and ambiguous identity are
never silently converted into a claim that two requirements are equivalent.
"""

import hashlib
import re
from difflib import SequenceMatcher

VERSION = "requirements-v2"
MODAL = re.compile(r"\b(shall|must|should)\b", re.I)
IDENTIFIER = re.compile(
    r"\b(?:REQ[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*|[A-Z]{2,12}-\d{2,}(?:-\d+)*)\b", re.I
)
NUMBER = r"[-+]?\d+(?:\.\d+)?"
UNITS = {
    "c": ("temperature", 1, 0),
    "celsius": ("temperature", 1, 0),
    "k": ("temperature", 1, -273.15),
    "kelvin": ("temperature", 1, -273.15),
    "f": ("temperature", 5 / 9, -160 / 9),
    "fahrenheit": ("temperature", 5 / 9, -160 / 9),
    "pa": ("pressure", 1, 0),
    "kpa": ("pressure", 1000, 0),
    "mpa": ("pressure", 1e6, 0),
    "v": ("voltage", 1, 0),
    "mv": ("voltage", 0.001, 0),
    "a": ("current", 1, 0),
    "ma": ("current", 0.001, 0),
    "w": ("power", 1, 0),
    "kw": ("power", 1000, 0),
    "hz": ("frequency", 1, 0),
    "khz": ("frequency", 1000, 0),
    "mhz": ("frequency", 1e6, 0),
    "s": ("time", 1, 0),
    "seconds": ("time", 1, 0),
    "ms": ("time", 0.001, 0),
    "mm": ("length", 0.001, 0),
    "cm": ("length", 0.01, 0),
    "m": ("length", 1, 0),
    "%": ("percentage", 1, 0),
}
MEASURE = re.compile(
    rf"({NUMBER})\s*°?\s*((?i:Celsius|Fahrenheit|Kelvin|seconds)|MPa|kPa|Pa|mV|mA|kW|MHz|kHz|Hz|ms|mm|cm|[CKFVAWsm%])(?=\W|$)",
)


def normalized(text):
    return " ".join(text.casefold().split())


def constraint(text):
    """A conservative single quantity constraint; compound expressions remain manual."""
    measures = list(MEASURE.finditer(text))
    if len(measures) != 1 or re.search(r"±|\+/-|\b(?:unless|except|or)\b", text, re.I):
        return None
    match = measures[0]
    if len(re.findall(NUMBER, IDENTIFIER.sub("", text))) != 1:
        return None
    if match[2] in {"C", "K", "F"} and "°" not in match[0]:
        if not re.search(r"\b(?:temperature|thermal|degrees?)\b", text, re.I):
            return None
    number, unit = float(match[1]), match[2].casefold()
    dimension, scale, offset = UNITS[unit]
    value = round(number * scale + offset, 9)
    before = normalized(text[: match.start()])
    after = normalized(text[match.end() :])
    if re.search(r"\bnot\b", before) and "not exceed" not in before:
        return None
    if after.startswith(("/", "^", "²", "³")):
        return None
    condition_match = re.search(r"\b(?:when|while|if|during|at)\b.*", after)
    condition = condition_match[0] if condition_match else ""
    # Conditions elsewhere in the statement cannot be interpreted with this grammar.
    if re.search(r"\b(?:when|while|if|during)\b", before):
        return None
    if re.search(r"(?:at most|not exceed|no more than|maximum|<=|≤)", before):
        lower, upper, lower_closed, upper_closed = None, value, False, True
    elif re.search(r"(?:below|less than|<)\s*$", before):
        lower, upper, lower_closed, upper_closed = None, value, False, False
    elif re.search(r"(?:at least|no less than|minimum|>=|≥)", before):
        lower, upper, lower_closed, upper_closed = value, None, True, False
    elif re.search(r"(?:above|greater than|>)\s*$", before):
        lower, upper, lower_closed, upper_closed = value, None, False, False
    elif re.search(r"(?:be|equal|exactly|=)\s*$", before):
        lower, upper, lower_closed, upper_closed = value, value, True, True
    else:
        return None
    return {
        "dimension": dimension,
        "lower": lower,
        "upper": upper,
        "lower_closed": lower_closed,
        "upper_closed": upper_closed,
        "condition": condition,
        "original_value": number,
        "original_unit": match[2],
    }


def inventory(chunks, document):
    requirements, seen, warnings = [], set(), list(document.get("warnings", []))
    processed, empty, unsupported = 0, 0, 0
    for chunk in chunks:
        if len(requirements) >= 1000:
            warnings.append("Requirement inventory capped at 1,000; remaining chunks need review")
            break
        processed += 1
        text = chunk["text"]
        if not text.strip():
            empty += 1
        warnings.extend(chunk.get("warnings", []))
        # Sentence boundaries preserve offsets, line wraps and decimal quantities.
        for match in re.finditer(r".+?(?:[.!?](?=\s+[A-Z]|\s*$)|\n{2,}|$)", text, re.S):
            if len(requirements) >= 1000:
                warnings.append(
                    "Requirement inventory capped at 1,000; remaining text needs review"
                )
                break
            raw = match[0]
            quote = raw.strip()
            modal = MODAL.search(quote)
            if not modal or len(quote) < 12:
                continue
            signature = normalized(quote)
            if signature in seen:
                continue
            seen.add(signature)
            start = match.start() + len(raw) - len(raw.lstrip())
            explicit = IDENTIFIER.search(quote)
            # A separated identifier in the immediately preceding paragraph is retained.
            if not explicit:
                prefix = text[max(0, start - 100) : start].strip()
                if IDENTIFIER.fullmatch(prefix.rstrip(":")):
                    explicit = IDENTIFIER.search(prefix)
            numeric = constraint(quote)
            if re.search(r"\d", quote) and numeric is None:
                unsupported += 1
            pages = sorted(
                {
                    source["page"]
                    for mapping in chunk["mappings"]
                    for source in mapping["sources"]
                    if source.get("page")
                    and mapping["output_start"] < start + len(quote)
                    and mapping["output_end"] > start
                }
            )
            identity = hashlib.sha256((document["sha256"] + signature).encode()).hexdigest()[:24]
            requirements.append(
                {
                    "key": identity,
                    "explicit_id": explicit[0].upper() if explicit else None,
                    "quote": quote,
                    "modality": modal[1].lower(),
                    "constraint": numeric,
                    "document_id": document["id"],
                    "document_sha256": document["sha256"],
                    "chunk_index": chunk["index"],
                    "chunk_id": chunk["chunk_id"],
                    "start": start,
                    "end": start + len(quote),
                    "pages": pages,
                    "headings": chunk.get("headings", []),
                    "mappings": [
                        mapping
                        for mapping in chunk["mappings"]
                        if mapping["output_start"] < start + len(quote)
                        and mapping["output_end"] > start
                    ],
                    "source_url": f"/?document={document['id']}&chunk={chunk['index']}"
                    + (f"&page={pages[0]}" if pages else ""),
                    "numeric_review": "parsed" if numeric else "manual",
                    "reference_validated": text[start : start + len(quote)] == quote,
                }
            )
    return {
        "document": document,
        "requirements": requirements,
        "coverage": {
            "processed_chunks": processed,
            "total_chunks": document["chunks"],
            "skipped_chunks": max(0, document["chunks"] - processed),
            "empty_chunks": empty,
            "requirements": len(requirements),
            "manual_numeric_review": unsupported,
            "warnings": warnings[:100],
            "exhaustive": False,
            "inventory_capped": len(requirements) >= 1000,
            "method": (
                "English shall/must/should sentences; implicit requirements "
                "and unsupported tables require human review"
            ),
        },
    }


def quantity_change(before, after):
    a, b = before.get("constraint"), after.get("constraint")
    if not a or not b or a["dimension"] != b["dimension"]:
        return "manual"
    if a["condition"] != b["condition"]:
        return "condition_changed"

    # Quantity comparison requires the same subject, modality and conditions.
    # Reusing an ID for a different property must never imply equivalence.
    def subject(item):
        text = IDENTIFIER.sub("", item["quote"])
        text = MEASURE.sub("<quantity>", text)
        return normalized(text)

    if subject(before) != subject(after):
        return "manual"
    fields = ("lower", "upper", "lower_closed", "upper_closed")
    if all(a[key] == b[key] for key in fields):
        return "equivalent_quantity"
    # Compare set containment only for a single bound with unchanged strictness.
    if a["lower"] is None and b["lower"] is None and a["upper_closed"] == b["upper_closed"]:
        return "tightened" if b["upper"] < a["upper"] else "relaxed"
    if a["upper"] is None and b["upper"] is None and a["lower_closed"] == b["lower_closed"]:
        return "tightened" if b["lower"] > a["lower"] else "relaxed"
    return "changed_quantity"


def compare(before, after, check=None):
    left, right = before["requirements"], after["requirements"]
    rows, used = [], set()
    left_ids, right_ids = {}, {}
    for item in left:
        if item["explicit_id"]:
            left_ids.setdefault(item["explicit_id"], []).append(item)
    for item in right:
        if item["explicit_id"]:
            right_ids.setdefault(item["explicit_id"], []).append(item)
    for old in left:
        if check:
            check()
        identity = old["explicit_id"]
        candidates = right_ids.get(identity, []) if identity else []
        duplicate = identity and (len(left_ids[identity]) > 1 or len(candidates) > 1)
        if duplicate:
            new, status, reason = (
                None,
                "ambiguous",
                "Duplicate requirement ID; select and review source passages manually",
            )
        elif len(candidates) == 1:
            new = candidates[0]
            status = (
                "unchanged" if normalized(old["quote"]) == normalized(new["quote"]) else "changed"
            )
            reason = "Explicit requirement ID"
        else:
            identical = [
                item
                for item in right
                if item["key"] not in used and normalized(item["quote"]) == normalized(old["quote"])
            ]
            if len(identical) == 1:
                new, status, reason = identical[0], "unchanged", "Exact normalized source quotation"
            else:
                scored = [
                    (
                        SequenceMatcher(
                            None, normalized(old["quote"]), normalized(item["quote"])
                        ).ratio(),
                        item,
                    )
                    for item in right
                    if item["key"] not in used and not identity and not item["explicit_id"]
                ]
                scored.sort(key=lambda pair: pair[0], reverse=True)
                if scored and scored[0][0] >= 0.8:
                    new, status, reason = (
                        scored[0][1],
                        "suggested_match",
                        "Similar wording without an explicit ID; human confirmation required",
                    )
                else:
                    new, status, reason = None, "removed", "No matching candidate requirement"
        if new:
            used.add(new["key"])
        rows.append(
            {
                "id": f"R{len(rows) + 1:04}",
                "status": status,
                "reason": reason,
                "before": old,
                "after": new,
                "quantity_change": quantity_change(old, new)
                if new and status == "changed"
                else "manual",
                "decision": "pending",
            }
        )
    for new in right:
        if new["key"] not in used:
            rows.append(
                {
                    "id": f"R{len(rows) + 1:04}",
                    "status": "added",
                    "reason": "No matched baseline requirement",
                    "before": None,
                    "after": new,
                    "quantity_change": "manual",
                    "decision": "pending",
                }
            )
    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in {row["status"] for row in rows}
    }
    return {
        "version": VERSION,
        "baseline": before,
        "candidate": after,
        "rows": rows,
        "counts": counts,
        "limitations": [
            "Findings require human review; this report is not a compliance certification.",
            "Numerical checks support a conservative single-quantity grammar. Unknown units, "
            "compound ranges and implicit requirements need manual review.",
            "Changed revision limits describe differences; they do not establish a "
            "contradiction between operating conditions.",
        ],
    }
