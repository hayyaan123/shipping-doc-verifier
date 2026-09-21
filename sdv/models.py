"""Shared data shapes. This is the contract between the pipeline and anything that displays its results."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# The seven compared fields, in report order.
FIELDS = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]

# Document types decided from CONTENT (never from the file name).
DOC_SI, DOC_BL = "SI", "BL"
DOC_INVOICE, DOC_PACKING, DOC_COO, DOC_UNKNOWN = "COMMERCIAL_INVOICE", "PACKING_LIST", "CERTIFICATE_OF_ORIGIN", "UNKNOWN"
WRONG_DOC_TYPES = {DOC_INVOICE, DOC_PACKING, DOC_COO}

# Three verdicts, never two: a blank/unreadable value is uncertainty, not a discrepancy.
MATCH, MISMATCH, UNCERTAIN = "match", "mismatch", "uncertain"

# Escalation reasons (first that applies wins, in this priority order).
UNREADABLE, WRONG_DOC_TYPE, MISSING_ATTACHMENT, MISSING_VALUE = (
    "unreadable",
    "wrong_doc_type",
    "missing_attachment",
    "missing_value",
)
REVIEW_PRIORITY = [UNREADABLE, WRONG_DOC_TYPE, MISSING_ATTACHMENT, MISSING_VALUE]

OK, NEEDS_REVIEW = "OK", "NEEDS_REVIEW"


@dataclass
class ExtractedField:
    """One value read from one document, with the evidence needed to audit it."""

    field: str
    value: Any = None  # normalized + typed (float for weight, int for containers, str/tuple otherwise)
    raw: str = ""  # exactly as the document wrote it
    label: str = ""  # the label it appeared under
    provenance: str = "native"  # native | ocr | vision  (drives comparison tolerance)
    confidence: float = 1.0
    source: str = ""  # e.g. "email_059_BL.pdf:12 (Consignee (Non-Negotiable))"
    blank: bool = False  # label present but value empty / placeholder (N/A, TBA, ____MT ...)
    found: bool = False  # label located at all


@dataclass
class DocRecord:
    """A read + typed + extracted document."""

    name: str
    doc_type: str = DOC_UNKNOWN
    readable: bool = True
    error: Optional[str] = None  # why it could not be read (corrupt / no text layer / ...)
    kind: str = "txt"  # txt | pdf | docx | xlsx
    text_layer: bool = True
    fields: dict = field(default_factory=dict)  # field name -> ExtractedField
    notes: list = field(default_factory=list)


@dataclass
class FieldComparison:
    field: str
    verdict: str  # match | mismatch | uncertain
    si_value: Optional[str] = None  # human-readable, as written in the SI
    bl_value: Optional[str] = None  # human-readable, as written in the BL
    reason: str = ""  # why uncertain / what differed
    si_source: str = ""
    bl_source: str = ""
    # For an uncertain field: which of the four escalation reasons it maps to (compare.py / pipeline.py decide).
    review_reason: Optional[str] = None


@dataclass
class EmailResult:
    email_id: str
    subject: str = ""
    category: str = "GENERAL"
    category_method: str = "rules"  # rules | jev | rules+jev
    category_confidence: float = 1.0
    status: str = OK  # OK | MISMATCH | NEEDS_REVIEW
    review_reason: Optional[str] = None
    has_defect: bool = False
    defect_fields: list = field(default_factory=list)
    comparisons: list = field(default_factory=list)  # list[FieldComparison]
    summary: str = ""  # one plain-English line for the report
    evidence: dict = field(default_factory=dict)  # doc types, notes, per-document errors
    error: Optional[str] = None  # processing failure (retryable), distinct from needs-review

    def to_submission(self) -> dict:
        """The exact shape the self-evaluation endpoint expects."""
        return {
            "category": self.category,
            "status": self.status,
            "review_reason": self.review_reason,
            "has_defect": self.has_defect,
            "defect_fields": list(self.defect_fields),
        }

    def to_dict(self) -> dict:
        return asdict(self)
