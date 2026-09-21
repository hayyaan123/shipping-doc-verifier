"""Where does a value END, and where does it BEGIN when the label line is empty?

Both extraction paths (extract.py and verify.py) call these two functions, so they can never disagree
about a column split. Independence between the paths is in HOW THEY FIND THE LABEL, not in this rule.

value_from_tail   a line can hold two columns ("Shipper: ACME LLC     B/L No: 123"). The value stops where a
                  right-hand column starts (a known label, or "Something:"), and is never cut because a word
                  such as BL or booking appears inside a company name, or because the value has a wide gap.
next_line_value   the label line is empty and the value sits on the next line. Only taken when that line is
                  plausibly a value: not another label, not "MEASUREMENT: 45.5 CBM", and for the numeric fields
                  something that actually looks like that number.
"""
from __future__ import annotations

import re

from .labels import _bare_label, asciify, looks_like_label

_LEAD = re.compile(r"^[\s:\-\u2013\u2014]+")
_GAP = re.compile(r"\t|\s{2,}")
_UNKNOWN_COL = re.compile(r"^[A-Za-z][A-Za-z0-9 /().'\-]{1,30}:(?:\s|$)")
# A well-known label glued to the value by a single space: "ACME LLC Booking No: 5". Requires the colon.
_INLINE = re.compile(r"\s+(?=(?:booking|b/?l|vessel|voy(?:age)?|hs\s*code|oc\s*no|invoice)\b[^:\n]{0,15}:)", re.I)
_WEIGHT_LINE = re.compile(r"^[\d][\d.,' \u00a0\u202f]*\s*(?:kgs?|kilo(?:gram)?s?|mts?|tonnes?|tons?)?\.?$", re.I)


def _is_column_label(seg: str) -> bool:
    a = asciify(seg).strip()
    return looks_like_label(seg) or bool(_UNKNOWN_COL.match(a))


def value_from_tail(tail: str) -> str:
    """The value that follows a label, with any right-hand column removed."""
    tail = tail.replace("\u00a0", " ")
    gap_first = bool(re.match(r"^[:\-\u2013\u2014]?\s{2,}", tail))  # the value is separated from the label by a wide gap
    tail = _LEAD.sub("", tail) if tail.strip() else ""
    m = _INLINE.search(tail)
    if m:
        tail = tail[: m.start()]
    parts = [p for p in _GAP.split(tail) if p.strip()]
    keep = []
    for i, p in enumerate(parts):
        if i > 0 and _is_column_label(p):
            break
        if i == 0 and gap_first and _bare_label(p):
            break  # "Shipper:      CONSIGNEE": a header cell, not the value
        if i == 0 and (looks_like_label(p) or _UNKNOWN_COL.match(asciify(p).strip())) and ":" in p[:40] and gap_first:
            break  # the value is empty and the next column has already begun
        keep.append(p.strip())
    return " ".join(keep).strip()


def next_line_value(lines: list, i: int, field: str) -> str:
    """The value on the line after an empty label line ('' when the next line is not a plausible value)."""
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return ""
    nxt = lines[j].strip()
    a = asciify(nxt).strip()
    if looks_like_label(nxt) or _UNKNOWN_COL.match(a) or nxt.endswith(":"):
        return ""
    v = value_from_tail(nxt)
    if field == "gross_weight_kg" and not _WEIGHT_LINE.match(asciify(v).strip()):
        return ""  # e.g. "45.5 CBM": a measurement, not a weight
    if field == "container_count" and not re.match(r"^\d", v):
        return ""
    return v
