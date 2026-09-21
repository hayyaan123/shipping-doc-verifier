"""Label vocabulary: which document label means which canonical field.

Alignment is by meaning, not header text. These patterns cover every label variant in the sample
corpus ("Load Port" / "Port of Loading (POL)" / "POL", "To the Order of" -> consignee, ...). A label
that matches none of them is handed to the label resolver (Jev, when configured) in extract.py.
"""
from __future__ import annotations

import re

# Field -> regex fragment that matches the label at the START of a line (case-insensitive).
# Order matters where labels overlap: notify is tested before consignee because
# "Notify Party/Intermediate Consignee" contains the word consignee.
LABEL_PATTERNS = {
    "notify_party": r"notify(?:\s+party)?(?:\s*/\s*intermediate\s+consignee)?",
    "consignee": r"consignee(?:\s*\(\s*non[\s-]*negotiable\s*\))?|to\s+the\s+order\s+of",
    "shipper": r"shipper(?:\s*/\s*exporter)?(?:\s*\(\s*principal\s+or\s+seller\s*\))?|exporter",
    "port_of_loading": r"port\s+of\s+loading(?:\s*\(\s*pol\s*\))?|load(?:ing)?\s+port|pol",
    "port_of_discharge": r"port\s+of\s+discharge(?:\s*\(\s*pod\s*\))?|discharge\s+port|pod",
    "container_count": r"(?:total\s+)?(?:no\.?\s+of\s+)?containers?(?:\s+or\s+packages)?(?:\s+count)?|container\s+count",
    # (?:\S{0,3}(?=...)) tolerates a glyph glued to the label ("Gross Weight■■(KGS)" reads as "Weightnn(KGS)" in some PDFs)
    "gross_weight_kg": r"(?:total\s+)?gross\s+(?:weight|wt)(?:\S{0,3}(?=\s*\(\s*kgs?\s*\)))?(?:\s*\(\s*kgs?\s*\))?",
}

# Labels we recognise but do not compare. Knowing them stops a value from swallowing the next line.
IGNORED_LABEL = re.compile(
    r"^(?:vessel(?:\s+name)?|ocean\s+vessel|export\s+carrier|voy(?:age)?(?:\.|\s+no\.?)?|hs\s+code|booking(?:\s+(?:ref(?:erence)?|no\.?))?|"
    r"oc\s+no\.?|freight|commodity|description(?:\s+of\s+goods)?|kinds\s+of\s+packages.*|net\s+weight|"
    r"b/?l(?:\s+no\.?|\s+number)?|bill\s+of\s+lading(?:\s+no\.?)?|order\s+no\.?|invoice.*|payment.*|incoterms|"
    r"seller|buyer|certificate.*|country\s+of\s+origin|issuing.*|container\s+no\.?|carton.*|total\s+amount)\b",
    re.I,
)

_COMPILED = {f: re.compile(r"^\s*(?:" + p + r")(?=$|[\s:\-\.])", re.I) for f, p in LABEL_PATTERNS.items()}
_MATCH_ORDER = ["notify_party", "consignee", "shipper", "port_of_loading", "port_of_discharge", "container_count", "gross_weight_kg"]

# Guard: the container TABLE header ("CONTAINER NO.  DESCRIPTION ...") must never read as a container count.
_TABLE_HEADER = re.compile(r"^\s*container\s*(?:(?:no|nos|num|number|numbers|id|ids|seal|size|type)\b|#)", re.I)
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
# A gloss is a parenthetical with non-ASCII text, or an EMPTY one left behind when a scan/PDF font drops the glyphs.
_GLOSS = re.compile(r"\s*\((?:\s*|[^)]*[^\x00-\x7f][^)]*)\)")


def asciify(line: str) -> str:
    """Replace non-ASCII characters (Chinese label glosses, box glyphs) with spaces, keeping string length."""
    return _NON_ASCII.sub(" ", line)


def match_label(line: str):
    """Return (field, end_index_of_label, label_text) if the line starts with a known label, else None."""
    a = asciify(line)
    if _TABLE_HEADER.match(a):
        return None
    for f in _MATCH_ORDER:
        m = _COMPILED[f].match(a)
        if m:
            end = m.end()
            # Swallow bilingual glosses such as "(收货人)" or "(毛重 KGS)" that sit between label and value:
            # any parenthetical group that contains a non-ASCII character is part of the label.
            while True:
                g = _GLOSS.match(line, end)
                if not g:
                    break
                end = g.end()
            return f, end, line[: m.end()].strip()
    return None


# Label occurrences ANYWHERE in a line (the second extraction path scans for these). Where two labels overlap
# ("Notify Party/Intermediate Consignee" contains "Consignee") the one earlier in _MATCH_ORDER wins.
_ANYWHERE = {f: re.compile(r"(?<![^\s(])(?:" + p + r")(?=$|[\s:\-.(])", re.I) for f, p in LABEL_PATTERNS.items()}


def find_labels(line: str) -> list:
    """-> [(field, start, end)] for every label occurrence in the line, in text order."""
    a = asciify(line)
    if _TABLE_HEADER.match(a):
        return []
    taken, out = [], []
    for f in _MATCH_ORDER:
        for m in _ANYWHERE[f].finditer(a):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append((f, m.start(), m.end()))
    return sorted(out, key=lambda t: t[1])


def label_end(line: str, end: int) -> int:
    """Skip bilingual glosses / empty parentheses that follow a label ending at `end`."""
    while True:
        g = _GLOSS.match(line, end)
        if not g:
            return end
        end = g.end()


def looks_like_label(line: str) -> bool:
    """True if the line starts a new labelled entry (compared or ignored)."""
    a = asciify(line).strip()
    if not a:
        return False
    return match_label(line) is not None or bool(IGNORED_LABEL.match(a))
