import io
import json
import urllib.error

from sdv import jev as jevmod
from sdv.jev import JevClient, triage_questions, triage_state
from sdv.triage import triage


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def answer(cat, conf, send=0.1):
    return {"answers": {"category": {"type": "choice", "choice": cat, "probabilities": {cat: conf}, "confidence": conf},
                        "asks_to_send_draft": {"type": "noul", "noul": send}}}


EMAIL = {"email_id": "e", "subject": "s", "body": "some unfamiliar text", "attachments": []}


def test_answers_are_cached_and_reused(tmp_path, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        return FakeResp(json.dumps(answer("GENERAL", 0.9)).encode())

    monkeypatch.setattr(jevmod.urllib.request, "urlopen", fake_urlopen)
    c = JevClient(api_key="k", cache_dir=str(tmp_path))
    a1 = c.ask(triage_state(EMAIL), triage_questions())
    a2 = c.ask(triage_state(EMAIL), triage_questions())
    assert a1 == a2 and len(calls) == 1 and c.cache_hits == 1


def test_auth_failure_disables_and_falls_back_to_rules(tmp_path, monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(jevmod.urllib.request, "urlopen", boom)
    c = JevClient(api_key="bad", cache_dir=str(tmp_path))
    assert c.ask(triage_state(EMAIL), triage_questions()) is None
    assert c.disabled_reason
    t = triage(EMAIL, c, "all")  # must not raise; rules answer
    assert t.method == "rules"


def test_no_key_means_rules_only(tmp_path):
    c = JevClient(api_key="", cache_dir=str(tmp_path))
    assert not c.available
    assert triage(EMAIL, c, "auto").method == "rules"


def test_jev_resolves_what_rules_are_unsure_of(tmp_path, monkeypatch):
    monkeypatch.setattr(jevmod.urllib.request, "urlopen", lambda req, timeout=0: FakeResp(json.dumps(answer("INVOICE_QUERY", 0.93)).encode()))
    c = JevClient(api_key="k", cache_dir=str(tmp_path))
    t = triage(EMAIL, c, "auto")  # rules give GENERAL at 0.5 -> Jev is consulted
    assert t.category == "INVOICE_QUERY" and t.method == "jev"


def test_confident_rules_are_not_overridden_in_auto_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(jevmod.urllib.request, "urlopen", lambda req, timeout=0: FakeResp(json.dumps(answer("GENERAL", 0.99)).encode()))
    c = JevClient(api_key="k", cache_dir=str(tmp_path))
    spam = {"email_id": "e", "subject": "WIN", "body": "You have won! http://x.example", "attachments": []}
    assert triage(spam, c, "auto").category == "SPAM"
    assert c.calls == 0  # auto mode did not spend a call on a confident rule


def test_env_key_lookup_accepts_either_name(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("Jev_api_key=abc123\n")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    assert jevmod.find_api_key(str(p)) == "abc123"
