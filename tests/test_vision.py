import json
import os

from sdv.inbox import Inbox
from sdv.pipeline import process_email
from sdv.vision import VisionReader


def test_vlm_request_is_openai_compatible_and_failures_return_none(tmp_path, monkeypatch):
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": "Consignee: ACME LTD\nPort of Loading: X"}}]}).encode()

    def fake(req, timeout=0):
        seen["url"], seen["auth"], seen["body"] = req.full_url, req.headers.get("Authorization"), json.loads(req.data)
        return R()

    import sdv.vision as v
    monkeypatch.setattr(v.urllib.request, "urlopen", fake)
    monkeypatch.setattr(v, "render_pages", lambda b, **k: [b"\x89PNG-fake"])
    r = VisionReader("vlm", api_key="k", cache_dir=str(tmp_path))
    assert r.read(b"pdf") == ["Consignee: ACME LTD", "Port of Loading: X"]
    assert seen["url"].endswith("/chat/completions") and seen["auth"] == "Bearer k"
    assert seen["body"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert r.read(b"pdf") is not None and r.cache_hits == 1  # second call served from cache

    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(v.urllib.request, "urlopen", boom)
    r2 = VisionReader("vlm", api_key="k", cache_dir=str(tmp_path / "x"))
    assert r2.read(b"other") is None and r2.failures == 1


def test_scan_hint_never_changes_the_verdict():
    data = os.environ.get("SDV_DATA")
    if not data:
        return
    inbox = Inbox(data)
    email = next(e for e in inbox.emails() if e["email_id"] == "email_512")
    plain = process_email(email, inbox, None, "off")

    class V:
        last_engine = "fake"
        def read(self, data):
            return ["BILL OF LADING (DRAFT)", "Consignee: ACME LTD"]

    hinted = process_email(email, inbox, None, "off", vision=V())
    assert (hinted.status, hinted.review_reason, hinted.defect_fields, hinted.has_defect) == \
           (plain.status, plain.review_reason, plain.defect_fields, plain.has_defect) == ("NEEDS_REVIEW", "unreadable", [], False)
    assert "scan_reading" in hinted.evidence and hinted.evidence["scan_reading"]["unverified"] is True
    assert "scan_reading" not in plain.evidence
