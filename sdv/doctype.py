"""Document identity from CONTENT, never from the file name.

A file called ..._BL.txt that opens with COMMERCIAL INVOICE is exactly the case we must catch.
Instruction wording is tested before bill-of-lading wording, because "BILL OF LADING INSTRUCTION"
is a Shipping Instruction.
"""
from __future__ import annotations

import re

from .models import DOC_BL, DOC_COO, DOC_INVOICE, DOC_PACKING, DOC_SI, DOC_UNKNOWN

_HEAD_LINES = 8

_RULES = [
    (DOC_INVOICE, re.compile(r"commercial\s+invoice|\binvoice\s+no\b", re.I)),
    (DOC_PACKING, re.compile(r"packing\s+list", re.I)),
    (DOC_COO, re.compile(r"certificate\s+of\s+origin", re.I)),
    # Instruction wording BEFORE bill-of-lading wording.
    (DOC_SI, re.compile(r"shipping\s+instruction|(?:bill\s+of\s+lading|b/?l|b\.l\.)\s+instruction|\[sheet\]\s*s\.?i\.?", re.I)),
    (DOC_BL, re.compile(r"bill\s+of\s+lading|\[sheet\]\s*b/?l\b", re.I)),
]


# A wrong-document marker in the BODY must be strong: a real BL mentions "Invoice No." as an ordinary field.
_BODY_MARKERS = [
    (DOC_INVOICE, re.compile(r"commercial\s+invoice", re.I)),
    (DOC_PACKING, re.compile(r"packing\s+list", re.I)),
    (DOC_COO, re.compile(r"certificate\s+of\s+origin", re.I)),
]
_TITLE_MAX_CHARS = 60


def _looks_like_title(line: str) -> bool:
    return len(line) <= _TITLE_MAX_CHARS and ":" not in line


def detect_doc_type(lines: list) -> str:
    """Decide the type from the title area, then from any title-like line further down, then from strong body markers."""
    nonempty = [l.strip() for l in lines if l.strip()]
    for line in nonempty[:_HEAD_LINES]:
        for dtype, rx in _RULES:
            if rx.search(line):
                return dtype
    # The title may sit below a letterhead / reference block: a SHORT line without a colon is a title, not a field.
    for line in nonempty[_HEAD_LINES:]:
        if _looks_like_title(line):
            for dtype, rx in _RULES[3:] + _RULES[:3]:
                if rx.search(line):
                    return dtype
    # Wrong-document footers ("THIS IS A COMMERCIAL INVOICE") anywhere in the text, but only strong markers.
    body = "\n".join(lines)
    for dtype, rx in _BODY_MARKERS:
        if rx.search(body):
            return dtype
    return DOC_UNKNOWN
