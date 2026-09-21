import json
import threading
import urllib.request

from sdv.cloud import pull_decisions, push_cases
from sdv.console import export_static, make_handler, render_index
from sdv.store import CaseStore

from http.server import ThreadingHTTPServer


def case(eid, status="NEEDS_REVIEW", cat="BL_COMPARISON", error=None):
    return {"email_id": eid, "subject": f"Subj {eid} </script>", "category": cat, "category_method": "rules",
            "category_confidence": 0.95, "status": status, "review_reason": "missing_value" if status == "NEEDS_REVIEW" else None,
            "has_defect": status == "MISMATCH", "defect_fields": ["consignee"] if status == "MISMATCH" else [],
            "comparisons": [], "summary": "s", "evidence": {}, "error": error}


def make_store(tmp_path):
    s = CaseStore(str(tmp_path / "c.db"))
    s.upsert_many([case("e1"), case("e2", "OK"), case("e3", "MISMATCH"), case("e4", "OK", "SPAM"), case("e5", "OK", error="Boom")])
    return s


def test_store_stats_and_review_flow(tmp_path):
    s = make_store(tmp_path)
    st = s.stats()
    assert st["emails"] == 5 and st["comparisons"] == 4 and st["awaiting_human"] == 2 and st["failed"] == 1
    s.review("e1", "confirmed_defect", "checked with carrier", "peshaant")
    assert s.get("e1")["review"]["decision"] == "confirmed_defect"
    assert s.stats()["awaiting_human"] == 1
    assert [c["email_id"] for c in s.list(pending_review=True)] == ["e3"]


def test_review_rejects_bad_decision_and_unknown_case(tmp_path):
    s = make_store(tmp_path)
    for bad in (lambda: s.review("e1", "nope"), lambda: s.review("zzz", "dismissed")):
        try:
            bad()
        except (ValueError, KeyError):
            continue
        raise AssertionError("expected an error")


def test_upsert_keeps_decision_when_case_is_reprocessed(tmp_path):
    s = make_store(tmp_path)
    s.review("e3", "dismissed")
    s.upsert(case("e3", "MISMATCH"))
    assert s.get("e3")["review"]["decision"] == "dismissed"


def test_static_export_cannot_break_out_of_script_tag(tmp_path):
    s = make_store(tmp_path)
    path = export_static(s, str(tmp_path / "site"))
    html = open(path, encoding="utf-8").read()
    assert "window.__SNAPSHOT__" in html
    assert "Subj e1 </script>" not in html  # escaped as <\/script>
    assert "__SNAPSHOT__*/" not in html


def test_http_api_round_trip(tmp_path):
    s = make_store(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        rows = json.load(urllib.request.urlopen(base + "/api/cases?status=MISMATCH"))
        assert [r["email_id"] for r in rows] == ["e3"]
        req = urllib.request.Request(base + "/api/cases/e3/review", data=json.dumps({"decision": "confirmed_defect"}).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        assert json.load(urllib.request.urlopen(req))["review"]["decision"] == "confirmed_defect"
        assert json.load(urllib.request.urlopen(base + "/api/stats"))["decided"] == 1
        assert "Shipping Desk" in urllib.request.urlopen(base + "/").read().decode()
    finally:
        srv.shutdown()


class FakeDoc:
    def __init__(self, id, d): self.id, self._d = id, d
    def to_dict(self): return self._d


class FakeFirestore:
    def __init__(self): self.docs = {}
    def collection(self, name): return self
    def document(self, id): return FakeRef(self.docs, id)
    def stream(self): return [FakeDoc(k, v) for k, v in self.docs.items()]


class FakeRef:
    def __init__(self, docs, id): self.docs, self.id = docs, id
    def set(self, d): self.docs[self.id] = d


def test_firestore_push_and_pull_decisions(tmp_path):
    s = make_store(tmp_path)
    fs = FakeFirestore()
    assert push_cases(s, client=fs) == 5
    fs.docs["e3"]["review"] = {"decision": "dismissed", "note": "teammate", "reviewer": "friend"}
    assert pull_decisions(s, client=fs) == 1
    assert s.get("e3")["review"]["note"] == "teammate"


def test_empty_or_wrong_data_folder_is_an_error_not_a_silent_zero(tmp_path):
    from sdv.inbox import Inbox

    (tmp_path / "bundle" / "inbox").mkdir(parents=True)
    for src in (tmp_path, tmp_path / "missing", tmp_path / "bundle"):
        try:
            Inbox(str(src)).emails()
        except FileNotFoundError as e:
            assert "inbox" in str(e)
            continue
        raise AssertionError("expected FileNotFoundError")


def test_stress_harness_runs_and_every_class_passes():
    import os
    import pytest

    data = os.environ.get("SDV_DATA")
    if not data:
        return
    from sdv.inbox import Inbox
    from sdv.stress import run_stress

    s = run_stress(Inbox(data), out_dir="out", limit=8)
    assert s["base_cases"] >= 1
    for cls, v in s["by_class"].items():
        assert v["passed"] == v["cases"], (cls, v)
