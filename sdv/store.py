"""Case store: one row per email, plus the human reviewer's decision.

SQLite (stdlib, zero setup) is the local store. `sdv/cloud.py` mirrors the same rows to Firestore.
Failures are first-class: a row with `error` set is a retryable processing failure, which is
different from a NEEDS_REVIEW verdict (that one is a deliberate hand-off to a person).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

DECISIONS = ("confirmed_defect", "dismissed", "reviewed_ok")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  email_id TEXT PRIMARY KEY, subject TEXT, category TEXT, category_method TEXT,
  category_confidence REAL, status TEXT, review_reason TEXT, has_defect INTEGER,
  defect_fields TEXT, summary TEXT, error TEXT, data TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS reviews (
  email_id TEXT PRIMARY KEY, decision TEXT, note TEXT, reviewer TEXT, decided_at REAL
);
"""


class CaseStore:
    def __init__(self, path: str = "out/cases.db"):
        self.path = path
        self._lock = threading.Lock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    # ---- writes -------------------------------------------------------------------------
    def upsert(self, r: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(email_id) DO UPDATE SET "
                "subject=excluded.subject, category=excluded.category, category_method=excluded.category_method, "
                "category_confidence=excluded.category_confidence, status=excluded.status, review_reason=excluded.review_reason, "
                "has_defect=excluded.has_defect, defect_fields=excluded.defect_fields, summary=excluded.summary, "
                "error=excluded.error, data=excluded.data, updated_at=excluded.updated_at",
                (r["email_id"], r.get("subject", ""), r.get("category"), r.get("category_method"),
                 r.get("category_confidence"), r.get("status"), r.get("review_reason"), int(bool(r.get("has_defect"))),
                 json.dumps(r.get("defect_fields") or []), r.get("summary", ""), r.get("error"),
                 json.dumps(r, ensure_ascii=False), time.time()),
            )

    def next_upload_id(self) -> str:
        row = self._db.execute("SELECT COUNT(*) FROM cases WHERE email_id LIKE 'upload_%'").fetchone()
        n = row[0] + 1
        while self.get(f"upload_{n:03d}") is not None:
            n += 1
        return f"upload_{n:03d}"

    def upsert_many(self, rows: list) -> None:
        for r in rows:
            self.upsert(r)

    def review(self, email_id: str, decision: str, note: str = "", reviewer: str = "") -> dict:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if self.get(email_id) is None:
            raise KeyError(email_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?) ON CONFLICT(email_id) DO UPDATE SET "
                "decision=excluded.decision, note=excluded.note, reviewer=excluded.reviewer, decided_at=excluded.decided_at",
                (email_id, decision, note, reviewer, time.time()),
            )
        return self.get(email_id)

    def clear_review(self, email_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM reviews WHERE email_id=?", (email_id,))

    # ---- reads --------------------------------------------------------------------------
    def _row(self, row) -> dict:
        d = json.loads(row["data"])
        d["review"] = (
            {"decision": row["decision"], "note": row["note"], "reviewer": row["reviewer"], "decided_at": row["decided_at"]}
            if row["decision"] else None
        )
        return d

    _JOIN = "SELECT c.data, v.decision, v.note, v.reviewer, v.decided_at FROM cases c LEFT JOIN reviews v USING(email_id)"

    def get(self, email_id: str) -> Optional[dict]:
        row = self._db.execute(self._JOIN + " WHERE c.email_id=?", (email_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, status: str = "", category: str = "", q: str = "", pending_review: bool = False) -> list:
        sql, args = self._JOIN + " WHERE 1=1", []
        if status:
            sql += " AND c.status=?"; args.append(status)
        if category:
            sql += " AND c.category=?"; args.append(category)
        if q:
            sql += " AND (c.email_id LIKE ? OR c.subject LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
        if pending_review:
            sql += " AND c.status IN ('NEEDS_REVIEW','MISMATCH') AND v.decision IS NULL"
        sql += " ORDER BY c.email_id"
        return [self._row(r) for r in self._db.execute(sql, args)]

    def failed(self) -> list:
        return [self._row(r) for r in self._db.execute(self._JOIN + " WHERE c.error IS NOT NULL ORDER BY c.email_id")]

    def stats(self) -> dict:
        rows = self.list()
        cmp_ = [r for r in rows if r["category"] == "BL_COMPARISON"]
        return {
            "emails": len(rows),
            "by_category": _count(r["category"] for r in rows),
            "comparisons": len(cmp_),
            "by_status": _count(r["status"] for r in cmp_),
            "by_review_reason": _count(r.get("review_reason") for r in cmp_ if r["status"] == "NEEDS_REVIEW"),
            "awaiting_human": sum(1 for r in cmp_ if r["status"] in ("NEEDS_REVIEW", "MISMATCH") and not r["review"]),
            "decided": sum(1 for r in cmp_ if r["review"]),
            "failed": sum(1 for r in rows if r.get("error")),
        }


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        if x:
            out[x] = out.get(x, 0) + 1
    return out


def retry_failed(store: CaseStore, inbox, jev=None, jev_mode: str = "auto") -> dict:
    """Re-run only the cases that failed processing. Verdict-level hand-offs are never retried."""
    from .pipeline import process_email

    wanted = {r["email_id"] for r in store.failed()}
    fixed = still = 0
    for email in inbox.emails():
        if email["email_id"] in wanted:
            res = process_email(email, inbox, jev, jev_mode)
            store.upsert(res.to_dict())
            fixed, still = (fixed + 1, still) if not res.error else (fixed, still + 1)
    return {"retried": fixed + still, "recovered": fixed, "still_failing": still}
