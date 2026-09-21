"""Stress test: does the verifier still behave when the documents are not the ones it was built on?

We take emails that the pipeline verifies as clean, then apply controlled edits to the draft BL and check the
verdict against what we KNOW the edit should cause. No answer key is involved: the expected outcome is
determined by the edit itself.

  benign   (must stay OK)           case, spacing, line endings, number formatting
  defect   (must be MISMATCH on exactly the edited field)
  missing  (must be NEEDS_REVIEW / missing_value)
  broken   (must be NEEDS_REVIEW with the matching reason: wrong doc, missing attachment, unreadable)

A "pass" requires the exact expected status, reason and defect fields. Reported per class so weak spots show.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .inbox import Inbox
from .labels import match_label
from .pipeline import process_email

_PARTY = ("shipper", "consignee", "notify_party")


class MemInbox:
    """Serves edited attachment bytes in front of a real inbox."""

    def __init__(self, base: Inbox):
        self.base, self.over = base, {}

    def emails(self):
        return self.base.emails()

    def read_bytes(self, path: str) -> bytes:
        return self.over[path] if path in self.over else self.base.read_bytes(path)


def _edit(text: str, field: str, fn):
    """Apply fn(old_value) -> new_value (or None to drop the line) to the first line labelled `field`."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = match_label(line)
        if m and m[0] == field:
            old = line[m[1]:].lstrip(" :").strip()
            new = fn(old)
            if new is not None and new.strip() == old.strip():
                return None  # the edit changed nothing: not a valid test case
            prefix = line[: m[1]].rstrip()
            if new is None:
                del lines[i]
            else:
                lines[i] = f"{prefix}: {new}" if not prefix.endswith(":") else f"{prefix} {new}"
            return "\n".join(lines)
    return None


def _num(s: str) -> float:
    m = re.search(r"[\d,]*\.?\d+", s)
    return float(m.group(0).replace(",", "")) if m else 0.0


# (class, name, expected_field or None, function(text) -> new text or None)
def _perturbations():
    P = []
    # --- benign: a human would say "same document" ---------------------------------------------------------------
    P.append(("benign", "party names lower-cased", None,
              lambda t: _all(t, _PARTY, lambda v: v.lower())))
    P.append(("benign", "extra spaces inside values", None,
              lambda t: _all(t, _PARTY, lambda v: "  ".join(v.split(" ")) + "  ")))
    P.append(("benign", "Windows line endings", None, lambda t: t.replace("\n", "\r\n")))
    P.append(("benign", "weight re-formatted (12,345 KG -> 12345.00 KGS)", None,
              lambda t: _edit(t, "gross_weight_kg", lambda v: f"{_num(v):.2f} KGS")))
    P.append(("benign", "port written in lower case", None,
              lambda t: _all(t, ("port_of_loading", "port_of_discharge"), lambda v: v.lower())))
    P.append(("benign", "weight written in tonnes (21.577 MT)", None,
              lambda t: _edit(t, "gross_weight_kg", lambda v: f"{_num(v) / 1000:.3f} MT")))
    # --- drift: wording the patterns have never seen. Pass = never a false MISMATCH (OK or hand-off both acceptable) ---
    for f, new in [("consignee", "Delivered To"), ("notify_party", "Also Advise"), ("gross_weight_kg", "Total Cargo Weight"),
                   ("container_count", "Equipment Loaded"), ("port_of_discharge", "Final Destination Port")]:
        P.append(("drift", f"{f} label renamed to '{new}'", None, lambda t, f=f, new=new: _relabel(t, f, new)))
    # --- defects: a person would call this a discrepancy -----------------------------------------------------------------
    for f, name, val in [("shipper", "shipper replaced", "ZEBRA LOGISTICS PTE LTD"),
                         ("consignee", "consignee replaced", "NORTHWIND TRADING GMBH"),
                         ("notify_party", "notify party replaced", "HARBOUR AGENCY SDN BHD"),
                         ("port_of_loading", "loading port replaced", "ROTTERDAM, NETHERLANDS"),
                         ("port_of_discharge", "discharge port replaced", "HAMBURG, GERMANY")]:
        P.append(("defect", name, f, lambda t, f=f, val=val: _edit(t, f, lambda v: val)))
    P.append(("defect", "container count +1", "container_count",
              lambda t: _edit(t, "container_count", lambda v: re.sub(r"\d+", lambda m: str(int(m.group(0)) + 1), v, count=1))))
    P.append(("defect", "weight +1 kg", "gross_weight_kg",
              lambda t: _edit(t, "gross_weight_kg", lambda v: f"{_num(v) + 1:,.0f} KG")))
    P.append(("defect", "weight +10%", "gross_weight_kg",
              lambda t: _edit(t, "gross_weight_kg", lambda v: f"{_num(v) * 1.1:,.0f} KG")))
    P.append(("defect", "consignee one-letter typo", "consignee",
              lambda t: _edit(t, "consignee", lambda v: v[:-1] + ("X" if v[-1] != "X" else "Y"))))
    # --- missing information ---------------------------------------------------------------------------------------------------
    for f in ("consignee", "port_of_discharge", "gross_weight_kg"):
        P.append(("missing", f"{f} left as 'N/A'", None, lambda t, f=f: _edit(t, f, lambda v: "N/A")))
        P.append(("missing", f"{f} line removed", None, lambda t, f=f: _edit(t, f, lambda v: None)))
    P.append(("missing", "weight left as '____ MT'", None, lambda t: _edit(t, "gross_weight_kg", lambda v: "____ MT")))
    return P


def _relabel(text: str, field: str, new_label: str):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = match_label(line)
        if m and m[0] == field:
            lines[i] = f"{new_label}: {line[m[1]:].lstrip(' :').strip()}"
            return "\n".join(lines)
    return None


def _all(text, fields, fn):
    out, changed = text, False
    for f in fields:
        n = _edit(out, f, fn)
        if n is not None:
            out, changed = n, True
    return out if changed else None


def _bl_path(email):
    return next((a for a in email["attachments"] if a.lower().endswith("_bl.txt")), None)


def run_stress(inbox: Inbox, out_dir: str = "out", limit=None, jev=None, jev_mode: str = "off") -> dict:
    mem = MemInbox(inbox)
    # eligible: clean comparisons whose two attachments are plain text
    base = []
    for e in inbox.emails():
        atts = e.get("attachments", [])
        if len(atts) == 2 and all(a.lower().endswith(".txt") for a in atts) and _bl_path(e):
            r = process_email(e, inbox, None, "off")
            if r.category == "BL_COMPARISON" and r.status == "OK" and r.comparisons:
                base.append(e)
    if limit:
        base = base[:limit]

    stats = defaultdict(lambda: {"n": 0, "pass": 0, "fails": [], "outcomes": {}})

    def record(cls, name, e, res, ok, why=""):
        s = stats[(cls, name)]
        s["n"] += 1
        s["pass"] += ok
        s["outcomes"][res.status] = s["outcomes"].get(res.status, 0) + 1
        if not ok and len(s["fails"]) < 5:
            s["fails"].append({"email_id": e["email_id"], "got": [res.status, res.review_reason, res.defect_fields], "why": why})

    for name_key in [(c, n) for c, n, *_ in _perturbations()]:
        stats[name_key]  # keep ordering

    for e in base:
        bl = _bl_path(e)
        original = inbox.read_bytes(bl).decode("utf-8", "replace")
        for cls, name, fld, fn in _perturbations():
            new = fn(original)
            if new is None:
                continue  # edit not applicable to this document
            mem.over = {bl: new.encode("utf-8")}
            res = process_email(e, mem, jev, jev_mode)
            if cls == "benign":
                ok = res.status == "OK"
            elif cls == "drift":
                ok = res.status != "MISMATCH"
            elif cls == "defect":
                ok = res.status == "MISMATCH" and res.defect_fields == [fld]
            else:
                ok = res.status == "NEEDS_REVIEW" and res.review_reason == "missing_value"
            record(cls, name, e, res, ok)

        # structural breakage: expected reason is known from the edit
        e2 = dict(e, attachments=[a for a in e["attachments"] if a != bl])
        mem.over = {}
        r = process_email(e2, mem, None, "off")
        record("broken", "BL attachment missing", e, r, r.status == "NEEDS_REVIEW" and r.review_reason == "missing_attachment")

        mem.over = {bl: re.sub(r"^BILL OF LADING[^\n]*", "COMMERCIAL INVOICE", original, count=1).encode()}
        r = process_email(e, mem, None, "off")
        record("broken", "BL replaced by a commercial invoice", e, r, r.status == "NEEDS_REVIEW" and r.review_reason == "wrong_doc_type")

        pdf_name = bl.replace(".txt", ".pdf")
        e3 = dict(e, attachments=[pdf_name if a == bl else a for a in e["attachments"]])
        mem.over = {pdf_name: b"%PDF-1.4\nnot really a pdf"}
        r = process_email(e3, mem, None, "off")
        record("broken", "BL is a corrupt PDF", e, r, r.status == "NEEDS_REVIEW" and r.review_reason == "unreadable")

    rows, by_class = [], defaultdict(lambda: [0, 0])
    for (cls, name), s in stats.items():
        if s["n"]:
            rows.append({"class": cls, "perturbation": name, "cases": s["n"], "passed": s["pass"],
                         "rate": round(s["pass"] / s["n"], 4), "outcomes": s["outcomes"], "failures": s["fails"]})
            by_class[cls][0] += s["n"]
            by_class[cls][1] += s["pass"]
    summary = {"base_cases": len(base), "by_class": {c: {"cases": n, "passed": p, "rate": round(p / n, 4)} for c, (n, p) in by_class.items()},
               "rows": rows}
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(out_dir, "stress.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def format_summary(s: dict) -> str:
    out = [f"Stress test on {s['base_cases']} verified-clean emails", ""]
    for c, v in s["by_class"].items():
        out.append(f"  {c:8} {v['passed']}/{v['cases']}  ({v['rate']:.1%})")
    out.append("")
    for r in s["rows"]:
        flag = "" if r["passed"] == r["cases"] else "   <-- check"
        if r["class"] == "drift":
            flag += "   outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(r["outcomes"].items()))
        out.append(f"  [{r['class']:7}] {r['perturbation']:48} {r['passed']:3}/{r['cases']:<3}{flag}")
    return "\n".join(out)
