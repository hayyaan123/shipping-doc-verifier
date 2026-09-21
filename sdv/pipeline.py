"""The pipeline: email -> category -> (for comparison requests) read, extract, compare, escalate.

Control flow is deterministic. Models appear at exactly the decisions with a small answer space
(triage, unfamiliar labels), each behind a cache and a rule fallback. Comparison is plain code.
"""
from __future__ import annotations

from typing import Callable, Optional

from . import verify
from .compare import compare_docs, label_for
from .doctype import detect_doc_type
from .extract import build_record
from .models import (DOC_BL, DOC_SI, DOC_UNKNOWN, FIELDS, MATCH, MISMATCH, MISSING_ATTACHMENT, MISSING_VALUE, NEEDS_REVIEW,
                     OK, REVIEW_PRIORITY, UNCERTAIN, UNREADABLE, WRONG_DOC_TYPE, WRONG_DOC_TYPES, EmailResult)
from .readers import read_document
from .triage import triage


def _make_label_resolver(jev) -> Optional[Callable]:
    if jev is None:
        return None
    from .jev import label_questions, label_state

    def resolve(label: str, value: str) -> Optional[str]:
        ans = jev.ask(label_state(label, value), label_questions(label, value))
        got = jev.choice(ans, "field") if ans else None
        if got and got[1] >= 0.8 and got[0] in FIELDS:
            return got[0]
        return None

    return resolve


def _load_docs(email: dict, inbox, resolver):
    """Read every attachment; return (records, texts). File NAMES are only a last-resort hint."""
    records, texts = [], []
    for path in email.get("attachments", []):
        name = path.split("/")[-1]
        try:
            data = inbox.read_bytes(path)
        except Exception as e:  # retrievable failure, surfaced rather than swallowed
            from .readers import DocText

            t = DocText("txt", readable=False, error=f"could not fetch attachment ({type(e).__name__})", text_layer=False)
        else:
            t = read_document(name, data)
        dtype = detect_doc_type(t.lines) if t.readable else DOC_UNKNOWN
        rec = build_record(name, t, dtype, resolver)
        records.append(rec)
        texts.append(t)
    return records, texts


def process_email(email: dict, inbox, jev=None, jev_mode: str = "auto") -> EmailResult:
    eid = email["email_id"]
    res = EmailResult(email_id=eid, subject=email.get("subject", ""))
    tri = triage(email, jev, jev_mode)
    res.category, res.category_confidence, res.category_method = tri.category, tri.confidence, tri.method
    res.evidence["triage_notes"] = tri.notes

    if res.category != "BL_COMPARISON":
        res.summary = f"Classified as {res.category}; no document check needed."
        return res

    n_att = len(email.get("attachments", []))
    if n_att < 2:
        if tri.send_draft:
            res.summary = "Request to send the draft BL; nothing to compare yet."
            res.evidence["note"] = "send-draft request (no attachments expected)"
            return res
        return _review(res, MISSING_ATTACHMENT, f"A comparison was requested but only {n_att} of 2 documents arrived.")

    resolver = _make_label_resolver(jev) if jev_mode != "off" else None
    records, texts = _load_docs(email, inbox, resolver)
    res.evidence["documents"] = [
        {"name": r.name, "type": r.doc_type, "readable": r.readable, "error": r.error, "text_layer": r.text_layer} for r in records
    ]

    reasons = {}
    # 1. unreadable: corrupt, or no text layer (scan). A person decides; we do not guess.
    bad = [r for r in records if not r.readable]
    if bad:
        reasons[UNREADABLE] = "; ".join(f"{r.name}: {r.error}" for r in bad)
    # 2. wrong document type, decided from content.
    wrong = [r for r in records if r.readable and r.doc_type in WRONG_DOC_TYPES]
    if wrong:
        reasons[WRONG_DOC_TYPE] = "; ".join(f"{r.name} is a {r.doc_type.replace('_', ' ').lower()}, not a shipping document" for r in wrong)
    # Pair by content; the file name is used only if the content gives no title at all.
    si = [r for r in records if r.readable and r.doc_type == DOC_SI]
    bl = [r for r in records if r.readable and r.doc_type == DOC_BL]
    for r in records:
        if r.readable and r.doc_type == DOC_UNKNOWN:
            hint = DOC_SI if "_si" in r.name.lower() else DOC_BL if "_bl" in r.name.lower() else None
            r.notes.append(f"document type not recognised from content; file name suggests {hint}")
            (si if hint == DOC_SI else bl).append(r) if hint else None
    if not reasons and (len(si) != 1 or len(bl) != 1):
        reasons[WRONG_DOC_TYPE] = f"expected one SI and one BL, found {len(si)} SI and {len(bl)} BL"

    if reasons:
        reason = next(k for k in REVIEW_PRIORITY if k in reasons)
        return _review(res, reason, reasons[reason])

    si_rec, bl_rec = si[0], bl[0]
    comps = compare_docs(si_rec, bl_rec)

    # Verification: a claimed mismatch must survive a DIFFERENT extraction path.
    lines_si = texts[records.index(si_rec)].lines
    lines_bl = texts[records.index(bl_rec)].lines
    for c in comps:
        if c.verdict == MISMATCH:
            ok, detail = verify.confirm(lines_si, lines_bl, c.field, si_rec.fields[c.field].value, bl_rec.fields[c.field].value)
            if not ok:
                c.verdict, c.reason = UNCERTAIN, f"mismatch not confirmed: {detail}"
    res.comparisons = comps

    uncertain = [c for c in comps if c.verdict == UNCERTAIN]
    mism = [c for c in comps if c.verdict == MISMATCH]
    if uncertain:
        blank = [c for c in uncertain if "blank" in c.reason or "not found" in c.reason]
        reason = MISSING_VALUE if blank else UNREADABLE
        detail = "; ".join(f"{label_for(c.field)}: {c.reason}" for c in uncertain)
        return _review(res, reason, detail, keep_comparisons=True)

    if mism:
        res.status = "MISMATCH"
        res.has_defect = True
        res.defect_fields = [c.field for c in mism]
        res.summary = "; ".join(f"{label_for(c.field)} - SI: {c.si_value} / BL: {c.bl_value}" for c in mism)
        return res

    res.status = OK
    res.summary = "No mismatch detected."
    return res


def _review(res: EmailResult, reason: str, detail: str, keep_comparisons: bool = False) -> EmailResult:
    res.status = NEEDS_REVIEW
    res.review_reason = reason
    res.has_defect = False
    res.defect_fields = []
    res.summary = f"Needs review ({reason}): {detail}"
    res.evidence["review_reason"] = reason
    res.evidence["review_detail"] = detail
    if not keep_comparisons:
        res.comparisons = []
    return res


def process_all(inbox, jev=None, jev_mode: str = "auto", limit: Optional[int] = None, progress=None) -> list:
    results = []
    for n, email in enumerate(inbox.emails()):
        if limit and n >= limit:
            break
        try:
            results.append(process_email(email, inbox, jev, jev_mode))
        except Exception as e:  # a processing FAILURE is visible and retryable, not a silent guess
            r = EmailResult(email_id=email["email_id"], subject=email.get("subject", ""))
            r.category = "GENERAL"
            r.error = f"{type(e).__name__}: {e}"
            r.summary = f"Processing failed (retryable): {r.error}"
            results.append(r)
        if progress:
            progress(n + 1)
    return results
