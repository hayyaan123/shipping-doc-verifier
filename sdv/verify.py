"""Verification by a DIFFERENT extraction path.

Re-running the same parser returns the same answer, so it proves nothing. The first path finds a
label at the start of a line and takes what follows. This second path ignores line structure: it
scans the raw text for every known label variant of the field, anywhere, and takes the value by
proximity (up to the next run of 2+ spaces or line end). A claimed mismatch is only printed if both
paths agree on both values. Disagreement -> uncertain, never an accusation.
"""
from __future__ import annotations

import re
from typing import Optional

from .labels import LABEL_PATTERNS, asciify, looks_like_label
from .normalize import is_blank, normalize

_STOP = re.compile(r"\s{3,}|\s+(?:booking|b/?l)\b|$", re.I)


def second_read(lines: list, field: str):
    """Independent value for `field`, or None if this path cannot find one."""
    rx = re.compile(r"(?:^|[\s(])(?:" + LABEL_PATTERNS[field] + r")\s*(?:\([^)]*\))?\s*[:\-]?\s+", re.I)
    alone = re.compile(r"^\s*(?:" + LABEL_PATTERNS[field] + r")\s*(?:\([^)]*\))?\s*[:\-]?\s*$", re.I)
    for idx, line in enumerate(lines):
        a = asciify(line)
        if alone.match(a):  # label on its own line: the value is the next non-empty line
            nxt = next((l for l in lines[idx + 1:] if l.strip()), "")
            if nxt.strip() and not looks_like_label(nxt) and not is_blank(nxt.strip()):
                val = normalize(field, nxt.strip())
                if val not in (None, "", ("", None)):
                    return val, nxt.strip()
            continue
        m = rx.search(a)
        if not m:
            continue
        # A container-table header must not count.
        if re.match(r"^\s*container\s+no\b", a, re.I):
            continue
        tail = line[m.end():]
        stop = _STOP.search(tail)
        cand = tail[: stop.start()] if stop else tail
        cand = cand.strip()
        if is_blank(cand) or (field == "container_count" and re.search(r"\b[A-Z]{4}\d{6,7}\b", cand)):
            continue
        val = normalize(field, cand)
        if val not in (None, "", ("", None)):
            return val, cand
    return None


def confirm(lines_si: list, lines_bl: list, field: str, si_value, bl_value) -> tuple:
    """-> (confirmed: bool, detail: str). Both documents must give the same values by the second path."""
    a = second_read(lines_si, field)
    b = second_read(lines_bl, field)
    if a is None or b is None:
        return False, "second extraction path could not locate the value"
    if a[0] != si_value or b[0] != bl_value:
        return False, f"second path read SI={a[1]!r} BL={b[1]!r}, which differs from the first read"
    return True, "confirmed by an independent extraction path"
