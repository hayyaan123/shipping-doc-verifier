"""Run health: signals that the input no longer looks like what the rules were built on.

The pipeline is validated on a templated development set. On real mail the risk is drift: new wording, layouts,
formats. These signals are computed from the run itself (no answer key needed) so a person can see, before trusting
the output, whether the system is coping or quietly escalating / disagreeing more than it should.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

# Baselines measured on the 520-email development set. A run far above them is a warning, not an error.
BASE_REVIEW_RATE = 0.09   # NEEDS_REVIEW share of comparisons (20/220)
WARN_REVIEW_RATE = 0.25
WARN_DISAGREE_RATE = 0.03


def run_health(results: list, jev=None) -> dict:
    cmp_ = [r for r in results if r.category == "BL_COMPARISON" and not r.error]
    review = [r for r in cmp_ if r.status == "NEEDS_REVIEW"]
    reasons = Counter(r.review_reason for r in review)
    disagree = [r for r in results
                if any("rules kept" in n or "overrides rules" in n for n in (r.evidence.get("triage_notes") or []))]
    label_events = [ev for r in results for ev in (r.evidence.get("label_resolutions") or [])]
    unresolved = [ev for ev in label_events if not ev["resolved_to"]]
    failed = [r for r in results if r.error]
    low_conf = [r for r in results if (r.category_confidence or 1) < 0.8]

    h = {
        "emails": len(results),
        "comparisons": len(cmp_),
        "review_rate": round(len(review) / len(cmp_), 4) if cmp_ else 0.0,
        "review_reasons": dict(reasons),
        "classifier_disagreements": [{"email_id": r.email_id, "category": r.category, "notes": r.evidence.get("triage_notes")} for r in disagree],
        "low_confidence_classifications": [r.email_id for r in low_conf],
        "unfamiliar_labels_seen": len(label_events),
        "unfamiliar_labels_unresolved": [{"label": e["label"], "value": e["value"]} for e in unresolved][:50],
        "processing_failures": [r.email_id for r in failed],
        "warnings": [],
    }
    w = h["warnings"]
    if cmp_ and h["review_rate"] > WARN_REVIEW_RATE:
        w.append(f"{h['review_rate']:.0%} of comparisons were escalated (development baseline {BASE_REVIEW_RATE:.0%}); "
                 f"the inputs may differ from what the rules were built on. Top reason: {reasons.most_common(1)[0][0] if reasons else 'n/a'}.")
    if results and len(disagree) / len(results) > WARN_DISAGREE_RATE:
        w.append(f"Jev and the rules disagreed on {len(disagree)} of {len(results)} emails; inspect them before trusting the categories.")
    if unresolved:
        w.append(f"{len(unresolved)} unfamiliar field labels could not be mapped; those documents were escalated.")
    if failed:
        w.append(f"{len(failed)} emails failed processing and can be retried.")
    if jev is not None and getattr(jev, "disabled_reason", None):
        w.append(f"Jev was disabled during the run ({jev.disabled_reason}); categories came from rules only.")
    return h


def write_health(h: dict, out_dir: str) -> str:
    p = Path(out_dir) / "health.json"
    p.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(p)


def format_health(h: dict) -> str:
    lines = [f"[health] escalation rate {h['review_rate']:.0%} of {h['comparisons']} comparisons | "
             f"classifier disagreements {len(h['classifier_disagreements'])} | unfamiliar labels {h['unfamiliar_labels_seen']} "
             f"({len(h['unfamiliar_labels_unresolved'])} unresolved) | failures {len(h['processing_failures'])}"]
    lines += [f"[health] WARNING: {w}" for w in h["warnings"]]
    return "\n".join(lines)
