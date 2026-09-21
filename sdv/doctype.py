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


def detect_doc_type(lines: list) -> str:
    """Look at the title area of the document (first non-empty lines)."""
    head = [l.strip() for l in lines if l.strip()][:_HEAD_LINES]
    for line in head:
        for dtype, rx in _RULES:
            if rx.search(line):
                return dtype
    # Fall back to the whole text for wrong-document markers (footers such as "THIS IS A COMMERCIAL INVOICE").
    body = "\n".join(lines)
    for dtype in (DOC_INVOICE, DOC_PACKING, DOC_COO):
        rx = dict(_RULES)[dtype]
        if rx.search(body):
            return dtype
    return DOC_UNKNOWN
