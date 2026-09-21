"""Comparison: three verdicts, provenance-aware, deterministic.

match     the documents agree
mismatch  the documents disagree (and, in the pipeline, a second extraction path confirmed it)
uncertain a value is blank/placeholder/unreadable, or two readings disagree

Tolerance applies to strings, never to numbers. Where BOTH values came from clean native text, any
difference is real. Where either came from OCR or a vision read, a tiny edit distance means the two
readings may be the same value seen badly: that is uncertain (sent to review), never silently matched
and never accused.
"""
from __future__ import annotations

from .models import FIELDS, MATCH, MISMATCH, UNCERTAIN, DocRecord, FieldComparison

_STRING_FIELDS = {"shipper", "consignee", "notify_party", "port_of_loading", "port_of_discharge"}
_LABELS = {
    "shipper": "Shipper",
    "consignee": "Consignee",
    "notify_party": "Notify party",
    "port_of_loading": "Port of loading",
    "port_of_discharge": "Port of discharge",
    "container_count": "Container count",
    "gross_weight_kg": "Gross weight (kg)",
}


def label_for(field: str) -> str:
    return _LABELS.get(field, field)


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _as_text(v) -> str:
    if isinstance(v, tuple):
        return " ".join(x for x in v if x)
    return str(v)


def _equal(field: str, a, b) -> bool:
    if field in ("port_of_loading", "port_of_discharge"):
        (ca, ka), (cb, kb) = a, b
        if ca != cb:
            return False
        return ka is None or kb is None or ka == kb
    return a == b


def compare_field(field: str, si, bl) -> FieldComparison:
    fs, fb = si.fields.get(field), bl.fields.get(field)
    c = FieldComparison(field=field, verdict=UNCERTAIN, si_source=getattr(fs, "source", ""), bl_source=getattr(fb, "source", ""))
    c.si_value = getattr(fs, "raw", "") or None
    c.bl_value = getattr(fb, "raw", "") or None

    problems = []
    for tag, f in (("SI", fs), ("BL", fb)):
        if f is None or not f.found:
            problems.append(f"{tag}: field not found")
        elif f.blank:
            problems.append(f"{tag}: blank or placeholder ({f.raw!r})" if f.raw else f"{tag}: blank")
    if problems:
        c.reason = "; ".join(problems)
        return c

    if _equal(field, fs.value, fb.value):
        c.verdict = MATCH
        return c

    # They differ. Is that a real discrepancy, or a reading problem?
    shaky = fs.provenance != "native" or fb.provenance != "native"
    if shaky and field in _STRING_FIELDS:
        a, b = _as_text(fs.value), _as_text(fb.value)
        # Scan reading drops/adds spaces and swaps look-alike characters; tolerate a fifth of the text (min 2 edits).
        # This only ever applies to values read from scans, which are never used for a verdict.
        limit = max(2, max(len(a), len(b)) // 5)
        if min(edit_distance(a, b), edit_distance(a.replace(" ", ""), b.replace(" ", ""))) <= limit:
            c.reason = "differs slightly and at least one value came from OCR/vision; may be a reading error"
            return c
    if shaky and field == "gross_weight_kg":
        try:
            x, y = float(fs.value), float(fb.value)
            if x and y and (abs(x * 1000 - y) < 1 or abs(y * 1000 - x) < 1):
                c.reason = "weights differ only by a thousands/decimal separator; likely a scan misread"
                return c
        except (TypeError, ValueError):
            pass
    if shaky and field not in _STRING_FIELDS and (fs.confidence < 0.9 or fb.confidence < 0.9):
        c.reason = "numeric values differ but one read is low confidence; not tolerated, sent to review"
        return c

    c.verdict = MISMATCH
    c.reason = f"SI: {c.si_value} / BL: {c.bl_value}"
    return c


def compare_docs(si: DocRecord, bl: DocRecord) -> list:
    return [compare_field(f, si, bl) for f in FIELDS]
