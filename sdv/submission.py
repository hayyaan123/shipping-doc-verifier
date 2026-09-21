"""Build the self-evaluation submission: {email_id: {category, status, review_reason, has_defect, defect_fields}}."""
from __future__ import annotations

import json
from pathlib import Path


def build_submission(results: list) -> dict:
    return {r.email_id: r.to_submission() for r in results}


def write_submission(results: list, path) -> dict:
    sub = build_submission(results)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(sub, indent=2), encoding="utf-8")
    return sub
