"""Jev audit: how often does the typed model agree with the rule tier, and where does it not?

This is the validation evidence for the AI tier. It asks Jev about EVERY email (answers are cached, so a
re-run is free) and compares its raw category with the rule tier. Disagreements are the interesting rows:
either the rules are brittle or Jev is wrong, and a person should look at each one.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .jev import triage_questions, triage_state
from .triage import rule_triage


def audit(inbox, jev, out_dir: str = "out", limit=None) -> dict:
    rows, agree = [], 0
    n = 0
    for email in inbox.emails():
        if limit and n >= limit:
            break
        n += 1
        rules = rule_triage(email)
        ans = jev.ask(triage_state(email), triage_questions())
        got = jev.choice(ans, "category") if ans else None
        if not got:
            rows.append({"email_id": email["email_id"], "subject": email.get("subject", ""), "rules": rules.category,
                         "rules_conf": rules.confidence, "jev": None, "jev_conf": None, "agree": None})
            continue
        ok = got[0] == rules.category
        agree += ok
        rows.append({"email_id": email["email_id"], "subject": email.get("subject", ""), "rules": rules.category,
                     "rules_conf": round(rules.confidence, 2), "jev": got[0], "jev_conf": round(got[1], 3), "agree": ok})
    answered = [r for r in rows if r["jev"] is not None]
    summary = {
        "emails": len(rows),
        "jev_answered": len(answered),
        "jev_unavailable": len(rows) - len(answered),
        "agree": agree,
        "agreement_rate": round(agree / len(answered), 4) if answered else None,
        "disagreements": [r for r in answered if not r["agree"]],
        "confusion": dict(Counter(f"{r['rules']} -> {r['jev']}" for r in answered if not r["agree"])),
        "jev_calls": jev.calls, "cache_hits": jev.cache_hits, "failures": jev.failures,
        "disabled_reason": jev.disabled_reason,
    }
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    Path(out_dir, "jev_audit.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
