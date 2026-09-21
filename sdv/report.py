"""Human-readable report + machine-readable results (the contract any console reads)."""
from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

from .compare import label_for
from .models import MISMATCH, NEEDS_REVIEW


def text_report(results: list) -> str:
    lines = []
    c = Counter((r.category, r.status) for r in results)
    lines.append(f"Emails processed: {len(results)}")
    lines.append("By category: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(r.category for r in results).items())))
    cmp_ = [r for r in results if r.category == "BL_COMPARISON"]
    lines.append(
        f"Document comparisons: {len(cmp_)}  |  OK={sum(r.status=='OK' for r in cmp_)}  MISMATCH={sum(r.status=='MISMATCH' for r in cmp_)}  "
        f"NEEDS_REVIEW={sum(r.status=='NEEDS_REVIEW' for r in cmp_)}"
    )
    lines.append("")
    for r in cmp_:
        lines.append(f"[{r.email_id}] {r.status}  {r.subject[:70]}")
        if r.status == MISMATCH:
            for cmp in r.comparisons:
                if cmp.verdict == "mismatch":
                    lines.append(f"    {label_for(cmp.field):20} SI: {cmp.si_value}   |   BL: {cmp.bl_value}")
        elif r.status == NEEDS_REVIEW:
            lines.append(f"    review reason: {r.review_reason} - {r.evidence.get('review_detail', '')}")
        else:
            lines.append(f"    {r.summary}")
    return "\n".join(lines) + "\n"


def write_outputs(results: list, out_dir) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "report.txt").write_text(text_report(results), encoding="utf-8")
    (out / "report.html").write_text(html_report(results), encoding="utf-8")


def html_report(results: list) -> str:
    rows = []
    for r in results:
        if r.category != "BL_COMPARISON":
            continue
        cls = {"OK": "ok", "MISMATCH": "bad", "NEEDS_REVIEW": "rev"}[r.status]
        detail = ""
        if r.status == MISMATCH:
            detail = "<table class='d'><tr><th>Field</th><th>SI</th><th>BL</th></tr>" + "".join(
                f"<tr><td>{html.escape(label_for(c.field))}</td><td>{html.escape(c.si_value or '')}</td><td>{html.escape(c.bl_value or '')}</td></tr>"
                for c in r.comparisons if c.verdict == "mismatch") + "</table>"
        elif r.status == NEEDS_REVIEW:
            detail = f"<b>{html.escape(r.review_reason or '')}</b>: {html.escape(r.evidence.get('review_detail', ''))}"
        else:
            detail = html.escape(r.summary)
        rows.append(f"<tr class='{cls}'><td>{html.escape(r.email_id)}</td><td>{html.escape(r.subject[:80])}</td><td>{r.status}</td><td>{detail}</td></tr>")
    return (
        "<!doctype html><meta charset='utf-8'><title>Shipping document verification</title>"
        "<style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}"
        ".ok td:nth-child(3){color:#1a7f37}.bad td:nth-child(3){color:#b42318;font-weight:600}.rev td:nth-child(3){color:#b54708;font-weight:600}.d{margin:0}</style>"
        "<h1>Shipping document verification</h1><table><tr><th>Email</th><th>Subject</th><th>Result</th><th>What needs attention</th></tr>"
        + "".join(rows) + "</table>"
    )
