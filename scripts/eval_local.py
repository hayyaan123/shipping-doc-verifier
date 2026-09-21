"""Score a submission against a local reference file and list disagreements (dev aid; needs the reference).

    python scripts/eval_local.py out/submission.json path/to/reference.json
Use it to find bugs, not to tune to individual emails.
"""
import json
import sys
from collections import Counter

sub = json.load(open(sys.argv[1]))
ref = json.load(open(sys.argv[2]))
bad = Counter()
rows = []
for eid, r in ref.items():
    s = sub.get(eid)
    if s is None:
        bad["missing"] += 1
        continue
    for k in ("category", "status", "review_reason"):
        if s.get(k) != r.get(k):
            bad[k] += 1
            rows.append((eid, k, s.get(k), r.get(k)))
    if sorted(s.get("defect_fields") or []) != sorted(r.get("defect_fields") or []):
        bad["defect_fields"] += 1
        rows.append((eid, "defect_fields", s.get("defect_fields"), r.get("defect_fields")))
print("disagreements by kind:", dict(bad))
for row in rows[: int(sys.argv[3]) if len(sys.argv) > 3 else 40]:
    print(row)
