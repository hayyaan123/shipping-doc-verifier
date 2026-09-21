from sdv.health import run_health
from sdv.models import EmailResult


def res(eid, cat="BL_COMPARISON", status="OK", reason=None, notes=None, err=None, ev=None):
    r = EmailResult(email_id=eid, category=cat, status=status, review_reason=reason)
    r.evidence = {"triage_notes": notes or [], **(ev or {})}
    r.error = err
    return r


def test_health_is_quiet_on_a_normal_run_and_loud_on_drift():
    ok = [res(f"e{i}") for i in range(20)] + [res("r1", status="NEEDS_REVIEW", reason="missing_value")]
    assert run_health(ok)["warnings"] == []

    drifted = [res(f"d{i}", status="NEEDS_REVIEW", reason="missing_value") for i in range(10)] + [res("ok1")]
    h = run_health(drifted)
    assert h["review_rate"] > 0.25 and any("escalated" in w for w in h["warnings"])


def test_health_reports_disagreements_labels_and_failures():
    rs = [res("a", notes=["jev suggested SPAM (0.91); rules kept"]), res("b", err="Boom"),
          res("c", ev={"label_resolutions": [{"label": "Ship-to", "value": "X", "resolved_to": None}]})] + [res(f"n{i}") for i in range(40)]
    h = run_health(rs)
    assert [d["email_id"] for d in h["classifier_disagreements"]] == ["a"]
    assert h["processing_failures"] == ["b"] and h["unfamiliar_labels_unresolved"][0]["label"] == "Ship-to"
    assert len(h["warnings"]) >= 2


def test_parallel_processing_keeps_inbox_order():
    from sdv.pipeline import process_all

    class Box:
        def emails(self):
            return [{"email_id": f"e{i:03d}", "subject": "hello there", "body": "thanks", "attachments": []} for i in range(30)]

    class J:  # any non-None jev enables the pool; never called in "off" mode
        pass

    out = process_all(Box(), J(), "off", workers=4)
    assert [r.email_id for r in out] == [f"e{i:03d}" for i in range(30)]
