"""Jev (TypeSafe AI System One) client: typed decisions with calibrated confidence.

Jev answers small typed questions (Choice / Score / Noul) about a piece of state. It does not read
documents into a schema and it does not generate text, so here it is used for the decisions with a
small answer space: which kind of email is this, is the sender asking for a draft to be SENT, which
canonical field does an unfamiliar label mean.

Design rules, so a rate limit or an outage costs accuracy and never the run:
  * every answer is cached on disk by a hash of the request (re-runs are free and reproducible);
  * every failure returns None and the caller falls back to the rule tier;
  * the API key is read from the environment or a git-ignored .env file, never from code.
Docs: https://docs.typesafe.ai  (POST https://api.typesafe.ai/v1/systemone, Bearer auth)
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

API_URL = "https://api.typesafe.ai/v1/systemone"
KEY_NAMES = ("jev_api_key", "typesafe_api_key")


def load_env(path: str = ".env") -> dict:
    """Minimal .env reader (KEY=VALUE lines). Values are returned, never printed or logged."""
    out = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_api_key(env_path: str = ".env") -> Optional[str]:
    merged = {k.lower(): v for k, v in {**load_env(env_path), **os.environ}.items()}
    for name in KEY_NAMES:
        if merged.get(name):
            return merged[name]
    return None


class JevClient:
    def __init__(self, api_key: Optional[str] = None, cache_dir: Optional[str] = None, model: str = "jev-latest",
                 timeout: float = 30.0, max_retries: int = 3):
        self.api_key = api_key if api_key is not None else find_api_key()
        # SDV_JEV_CACHE lets a deployment ship pre-computed answers (no key needed to serve them).
        self.cache_dir = Path(cache_dir or os.environ.get("SDV_JEV_CACHE") or ".cache/jev")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.disabled_reason: Optional[str] = None  # set after an auth/credit failure so we stop calling
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.disabled_reason is None

    # -- cache ----------------------------------------------------------------
    def _cache_path(self, payload: dict) -> Path:
        h = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.json"

    def ask(self, state, questions: dict) -> Optional[dict]:
        """Return {question_id: answer_dict} or None if unavailable. Never raises."""
        payload = {"model": self.model, "state": state, "questions": questions}
        cp = self._cache_path(payload)
        if cp.exists():
            try:
                answers = json.loads(cp.read_text(encoding="utf-8"))["answers"]
                self.cache_hits += 1
                return answers
            except Exception:
                pass
        if not self.available:
            return None
        data = json.dumps(payload).encode()
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(API_URL, data=data, headers={
                "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = json.loads(r.read())
                self.calls += 1
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                return body.get("answers")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    self.disabled_reason = f"authentication failed (HTTP {e.code}); check the key"
                    return None
                if e.code == 422:
                    self.failures += 1
                    return None  # our request was malformed for this question; do not retry
                if e.code in (429, 529, 500, 502, 503) and attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if e.code == 429:
                    self.disabled_reason = "rate limit / credits exhausted (HTTP 429)"
                self.failures += 1
                return None
            except Exception:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                self.failures += 1
                return None
        return None

    # -- typed helpers --------------------------------------------------------
    def choice(self, answers: Optional[dict], qid: str):
        """-> (choice, confidence) or None."""
        a = (answers or {}).get(qid)
        if not a or a.get("type") != "choice":
            return None
        return a.get("choice"), float(a.get("confidence", 0.0))

    def noul(self, answers: Optional[dict], qid: str) -> Optional[float]:
        a = (answers or {}).get(qid)
        return float(a["noul"]) if a and a.get("type") == "noul" else None


# ---------------------------------------------------------------------------
# The typed questions this system asks
# ---------------------------------------------------------------------------
CATEGORY_CRITERIA = {
    "BL_COMPARISON": "The sender wants a draft Bill of Lading checked against a Shipping Instruction, or asks for the draft BL to be sent for checking.",
    "SI_REQUEST": "The sender provides or forwards a Shipping Instruction so that a new Bill of Lading can be prepared.",
    "INVOICE_QUERY": "A question or request about an invoice, charges, goods receipt, or billing.",
    "GENERAL": "Routine operational information: reports, notifications, greetings, reminders, status updates.",
    "SPAM": "Unsolicited, scam, phishing or marketing mail.",
}


def triage_questions() -> dict:
    return {
        "category": {"type": "choice",
                     "instructions": "Which kind of message is this email?",
                     "criteria": CATEGORY_CRITERIA},
        "asks_to_send_draft": {"type": "noul",
                               "instructions": "Is the sender asking for the draft Bill of Lading to be SENT to them, rather than supplying documents to be checked?"},
    }


def triage_state(email: dict, max_body: int = 1500) -> dict:
    return {
        "subject": email.get("subject", ""),
        "body": (email.get("body") or "")[:max_body],
        "attachment_names": [a.split("/")[-1] for a in email.get("attachments", [])],
    }


LABEL_FIELD_CRITERIA = {
    "shipper": "the party sending or exporting the goods",
    "consignee": "the party the goods are consigned to (the receiver, or 'to the order of')",
    "notify_party": "the party to be notified on arrival",
    "port_of_loading": "the port where the cargo is loaded",
    "port_of_discharge": "the port where the cargo is unloaded",
    "container_count": "the number of containers",
    "gross_weight_kg": "the total gross weight of the cargo",
    "none": "none of the above (vessel, voyage, booking, description, HS code, reference numbers, etc.)",
}


def label_questions(label: str, value: str) -> dict:
    return {"field": {"type": "choice",
                      "instructions": "Which shipment field does the label of this document line refer to?",
                      "criteria": LABEL_FIELD_CRITERIA}}


def label_state(label: str, value: str) -> dict:
    return {"label": label, "example_value": value[:80]}
