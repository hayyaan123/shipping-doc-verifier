"""Verification by a DIFFERENT extraction path.

Re-running the same parser returns the same answer, so it proves nothing. The first path finds a
label at the start of a line and takes what follows. This second path ignores line structure: it
scans every line for every label occurrence, anywhere, in priority order. Where a value ends is one
shared rule (columns.py) so a column split can never make the two paths disagree. A claimed mismatch is
only printed if both paths agree on both values. Disagreement -> uncertain, never an accusation.
"""
from __future__ import annotations

import re
from typing import Optional

from .columns import next_line_value, value_from_tail
from .labels import find_labels, label_end
from .normalize import is_blank, normalize

_CONTAINER_ID = re.compile(r"\b[A-Z]{4}\d{6,7}\b")


def second_read_all(lines: list, field: str) -> list:
    """Every value this path finds for `field`, one per label occurrence, distinct, in document order.

    Reading ALL occurrences (not only the first) is what makes this path independent of the first one on
    wrong-LINE errors: the first path stops at the first occurrence, so if that line is the wrong one (a
    header row, a stray summary) the real line shows up here as a second, different value.

    Different from the first path in how it finds the label: it does not assume the label starts the line,
    it scans every line for every occurrence of every label (labels.find_labels, ordered so that
    "Notify Party/Intermediate Consignee" is a notify label, not a consignee one). Where a value ends is
    the shared columns.value_from_tail rule, so the two paths cannot disagree on a column split.
    Returns [(value, raw_text), ...].
    """
    found = []
    for idx, line in enumerate(lines):
        for f, _start, end in find_labels(line):
            if f != field:
                continue
            tail = line[label_end(line, end):]
            cand = value_from_tail(tail)
            if not cand and not tail.strip(" :-\u2013\u2014\t"):
                cand = next_line_value(lines, idx, field)  # label alone on its line
            if is_blank(cand) or (field == "container_count" and _CONTAINER_ID.search(cand)):
                continue
            val = normalize(field, cand)
            if val in (None, "", ("", None)):
                continue
            if all(val != v for v, _ in found):
                found.append((val, cand))
    return found


def second_read(lines: list, field: str):
    """First value the second path finds, or None."""
    got = second_read_all(lines, field)
    return got[0] if got else None


def confirm(lines_si: list, lines_bl: list, field: str, si_value, bl_value) -> tuple:
    """-> (confirmed: bool, detail: str). The second path must find exactly the first path's value in EACH document,
    and no other value for that field anywhere in it."""
    a = second_read_all(lines_si, field)
    b = second_read_all(lines_bl, field)
    if not a or not b:
        return False, "second extraction path could not locate the value"
    for tag, got, want in (("SI", a, si_value), ("BL", b, bl_value)):
        if len(got) > 1:
            return False, f"the {tag} gives more than one value for this field: " + " / ".join(repr(r) for _, r in got[:3])
        if got[0][0] != want:
            return False, f"second path read {tag}={got[0][1]!r}, which differs from the first read"
    return True, "confirmed by an independent extraction path"
