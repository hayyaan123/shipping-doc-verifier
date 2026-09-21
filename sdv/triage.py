"""Email triage: which of the five kinds of message is this, and (for comparisons) is the sender asking
us to CHECK documents or to SEND a draft?

Two tiers. Cheap rules run first and are exact on the templated sample corpus. A typed-decision model
(Jev) handles what the rules are not sure of, and in `all` mode double-checks everything, which is what
makes the classifier hold up on an inbox that no longer looks like the sample. Comparison intent is
decided separately from attachment count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .models import CATEGORIES

_BANNER = re.compile(r"^\s*(?:warning:\s*)?this email originated outside of our organi[sz]ation[^\n]*\n+", re.I)


def clean_body(body: str) -> str:
    """Strip the 'external email' warning banner that many messages start with."""
    return _BANNER.sub("", body or "", count=1).strip()


@dataclass
class Triage:
    category: str
    confidence: float
    method: str = "rules"  # rules | jev | rules+jev
    send_draft: bool = False  # BL_COMPARISON only: the sender asks for the draft BL to be sent
    notes: list = field(default_factory=list)


_AUTOMATED = re.compile(r"automated notification|rpa bot|no action required", re.I)
_SPAM = re.compile(
    r"bit\.ly|https?://|gift card|you have won|bank officer|bank details|business proposal|mailbox has exceeded|"
    r"verify your account|unpaid customs|\d+% off|buy now|claim your|winner|urgent transfer|prize",
    re.I,
)
_SEND_DRAFT = re.compile(r"(?:send|forward|provide|share)\b[^.\n]{0,25}\bdraft\s+(?:bl|bill of lading)", re.I)
_DRAFT_BL = re.compile(r"draft\s+(?:bl|b/l|bill of lading)", re.I)
_CHECK_WORDS = re.compile(r"check|compare|confirm|verify|discrepanc|revert|in order|for checking|advise", re.I)
_WRONG_DOC_COMPARE = re.compile(r"\bSI\b.{0,40}(?:commercial invoice|packing list|certificate of origin).{0,80}\bBL\b", re.I | re.S)
_SI_REQUEST = re.compile(r"please find (?:the )?shipping instruction", re.I)
_SI_WORDS = re.compile(r"shipping instruction", re.I)
_SI_SUBJECT = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)*request\s+si\b", re.I)
_INVOICE = re.compile(r"\binvoice|\bGR\b|\bTHC\b|D&D|detention|local charge|goods receipt|credit note|debit note|\bPGI\b", re.I)
_GENERAL_KNOWN = re.compile(
    r"berthing report|happy and prosperous|new year|outstanding bl|update summary|loading completed|"
    r"submit si & aed|reminder:|office resumes|documents to follow",
    re.I,
)


def rule_triage(email: dict) -> Triage:
    subject = email.get("subject", "") or ""
    body = clean_body(email.get("body", ""))
    text = f"{subject}\n{body}"
    atts = [a.lower() for a in email.get("attachments", [])]
    has_pair_names = any("_si." in a for a in atts) and any("_bl." in a for a in atts)

    if _AUTOMATED.search(body):
        return Triage("GENERAL", 0.95, notes=["automated notification"])
    if _SPAM.search(text):
        return Triage("SPAM", 0.95, notes=["spam markers"])

    # A supplied Shipping Instruction (possibly ending "please revert with draft BL once available") is a
    # request to PREPARE a BL, not a request to check one. Test it before the comparison wording.
    if _SI_REQUEST.search(body) or _SI_SUBJECT.search(subject):
        return Triage("SI_REQUEST", 0.95, notes=["shipping instruction supplied"])

    if _WRONG_DOC_COMPARE.search(body):
        return Triage("BL_COMPARISON", 0.9, notes=["asks to confirm the BL against the SI (second document is not a BL)"])
    if _DRAFT_BL.search(body) and _CHECK_WORDS.search(body):
        send = bool(_SEND_DRAFT.search(body)) and not has_pair_names
        return Triage("BL_COMPARISON", 0.95, send_draft=send, notes=["send-draft request" if send else "check/compare request"])
    if has_pair_names and _SI_WORDS.search(text + " si "):
        return Triage("BL_COMPARISON", 0.8, notes=["SI and BL attached"])

    if _SI_REQUEST.search(body):
        return Triage("SI_REQUEST", 0.95, notes=["shipping instruction supplied"])
    if _SI_WORDS.search(text) and not _DRAFT_BL.search(text):
        return Triage("SI_REQUEST", 0.75, notes=["mentions a shipping instruction"])

    # Body only: a subject line can mislead (an "RPA Billing Process" subject on a New Year greeting).
    if _INVOICE.search(body):
        return Triage("INVOICE_QUERY", 0.9, notes=["invoice/charges wording in the body"])
    if _GENERAL_KNOWN.search(text):
        return Triage("GENERAL", 0.9, notes=["routine operational message"])
    return Triage("GENERAL", 0.5, notes=["no rule matched"])


def triage(email: dict, jev=None, mode: str = "auto") -> Triage:
    """mode: off (rules only) | auto (Jev only where rules are unsure) | all (Jev on every email)."""
    from .jev import triage_questions, triage_state  # local import keeps the rule tier dependency-free

    rules = rule_triage(email)
    if mode == "off" or jev is None:
        return rules
    if mode == "auto" and rules.confidence >= 0.8:
        return rules

    answers = jev.ask(triage_state(email), triage_questions())
    got = jev.choice(answers, "category") if answers else None
    if not got or got[0] not in CATEGORIES:
        rules.notes.append("jev unavailable; rules used")
        return rules
    jev_cat, jev_conf = got
    send = jev.noul(answers, "asks_to_send_draft")
    send_draft = rules.send_draft if send is None else (send >= 0.5)

    if jev_cat == rules.category:
        return Triage(jev_cat, max(rules.confidence, jev_conf), "rules+jev", send_draft if jev_cat == "BL_COMPARISON" else False,
                      rules.notes + ["jev agrees"])
    # Disagreement: trust the typed model only when it is confident and the rules were not.
    if jev_conf >= 0.8 and (rules.confidence < 0.95 or mode == "all" and jev_conf >= 0.9):
        return Triage(jev_cat, jev_conf, "jev", send_draft if jev_cat == "BL_COMPARISON" else False,
                      rules.notes + [f"jev overrides rules ({rules.category})"])
    rules.notes.append(f"jev suggested {jev_cat} ({jev_conf:.2f}); rules kept")
    return rules
