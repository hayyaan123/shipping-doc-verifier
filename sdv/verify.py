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


def second_read(lines: list, field: str):
    """Independent value for `field`, or None if this path cannot find one.

    Different from the first path in how it finds the label: it does not assume the label starts the line,
    it scans every line for every occurrence of every label (labels.find_labels, ordered so that
    "Notify Party/Intermediate Consignee" is a notify label, not a consignee one). Where the value ends is
    the shared columns.value_from_tail rule, so the two paths cannot disagree on a column split.
    """
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
