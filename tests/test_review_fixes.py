"""Regression tests for bugs found in review. Most need TWO conditions at once (a defect plus a layout quirk)."""
import json
import os
import pathlib

from sdv.columns import next_line_value, value_from_tail
from sdv.compare import compare_field, edit_distance
from sdv.extract import extract_fields
from sdv.normalize import normalize_party, normalize_weight_kg
from sdv.pipeline import _safe_process, check_uploaded, process_all
from sdv.store import CaseStore
from sdv.triage import rule_triage
from sdv.verify import second_read

SI = """SHIPPING INSTRUCTION
Shipper/Exporter: APRIL FAR EAST (M) SDN BHD
CONSIGNEE: MOORIM SP CO., LTD
NOTIFY PARTY: UAB NOVAKOPA
Port of Loading: PORT KLANG (WESTPORT), MALAYSIA (MYPKG)
Discharge Port: CALLAO, PERU (PECLL)
No. of Containers or Packages: 1 x 40'HC
Gross Weight (KG): 21,577 KG
Vessel Name: MMSS 2507
"""
BL = """BILL OF LADING (DRAFT)
Shipper: APRIL FAR EAST (M) SDN BHD
Consignee: MOORIM SP CO., LTD
Notify Party: UAB NOVAKOPA
Port of Loading: PORT KLANG, MALAYSIA
Port of Discharge: CALLAO, PERU
Total Containers: 1
Gross Weight (KGS): 21,577 KGS
Booking No: 1234
"""


def check(si=SI, bl=BL):
    return check_uploaded([("x_SI.txt", si.encode()), ("x_BL.txt", bl.encode())])


def test_edit_distance_respects_the_callers_limit():
    assert edit_distance("ACME TRADING", "ZEBRA LOGISTICS GROUP") > 10  # exact when no cap is given
    assert edit_distance("abc", "abcdefgh", 3) == 4  # early exit only beyond the cap given
    v = lambda raw: type("R", (), {"fields": {"consignee": type("F", (), dict(value=normalize_party(raw), raw=raw, found=True, blank=False,
                                                                                provenance="vision", confidence=0.9, source=""))()}})()
    assert compare_field("consignee", v("ACME TRADING"), v("ZEBRA LOGISTICS GROUP")).verdict == "mismatch"
    assert compare_field("consignee", v("ACME TRADING"), v("ACME TRAD1NG")).verdict == "uncertain"  # a real OCR slip stays uncertain


def test_empty_label_does_not_swallow_the_next_line_even_when_every_line_is_indented():
    lines = ["  Gross Weight (KGS):", "  MEASUREMENT: 45.5 CBM", "  Notify Party: X"]
    f = extract_fields(lines, "d")["gross_weight_kg"]
    assert f.found and f.blank and f.value is None
    assert extract_fields(["  Gross Weight (KGS):", "  45.5 CBM"], "d")["gross_weight_kg"].blank
    assert extract_fields(["  Gross Weight (KGS):", "  12,345 KGS"], "d")["gross_weight_kg"].value == 12345.0  # a real value below still works
    assert next_line_value(["Total Containers:", "  PACKAGES: 12 PKGS"], 0, "container_count") == ""


def test_text_where_a_number_belongs_does_not_claim_the_field():
    lines = ["CONTAINER NUMBERS   SEAL   TYPE", "Container Count: 3"]
    f = extract_fields(lines, "d")["container_count"]
    assert f.value == 3 and not f.blank


def test_fetch_failure_is_a_retryable_failure_not_a_verdict(tmp_path):
    class Flaky:
        def emails(self):
            return [{"email_id": "e1", "subject": "check", "body": "Please check the draft BL against the SI", "attachments": ["a_SI.txt", "a_BL.txt"]}]

        def read_bytes(self, p):
            raise ConnectionResetError("boom")

    r = _safe_process(Flaky().emails()[0], Flaky(), None, "off", None)
    assert r.error and "ConnectionResetError" in r.error
    s = CaseStore(str(tmp_path / "c.db"))
    s.upsert_many([r.to_dict()])
    assert [c["email_id"] for c in s.failed()] == ["e1"]


def test_a_malformed_record_does_not_kill_the_run():
    class Box:
        def emails(self):
            return [{"subject": "no id"}, None, {"email_id": "ok1", "subject": "hi", "body": "Happy new year", "attachments": []}]

        def read_bytes(self, p):
            raise AssertionError

    res = process_all(Box(), None, "off")
    assert len(res) == 3 and res[0].error and res[1].error and res[2].email_id == "ok1" and not res[2].error
    assert len({r.email_id for r in res}) == 3


def test_legal_suffix_is_only_stripped_where_it_is_a_suffix():
    assert normalize_party("AS ONE TRADING") != normalize_party("ONE TRADING")
    assert normalize_party("ACME CO., LTD") == normalize_party("Acme") == "ACME"
    assert normalize_party("APRIL FAR EAST (M) SDN BHD") == normalize_party("April Far East (M)")
    assert normalize_party("PT INDO LTD") == "PT INDO"


def test_weight_unit_before_the_number():
    assert normalize_weight_kg("MT 22") == 22000.0 and normalize_weight_kg("22 MT") == 22000.0 and normalize_weight_kg("22 KG") == 22.0


def test_verification_reads_a_value_with_no_space_after_the_colon():
    assert second_read(["Consignee:MOORIM SP CO., LTD"], "consignee")[0] == normalize_party("MOORIM SP CO., LTD")


def test_column_split_is_shared_and_does_not_cut_names():
    assert value_from_tail(": ABC BL SHIPPING   CO LTD") == "ABC BL SHIPPING CO LTD"
    assert value_from_tail(": ACME LLC     B/L No: 123") == "ACME LLC"
    assert value_from_tail(": ACME LLC Booking No: 5") == "ACME LLC"
    assert value_from_tail("       Booking No: 5") == ""
    lines = ["Notify Party/Intermediate Consignee: HARBOUR AGENCY", "Consignee: REAL CONSIGNEE LTD"]
    assert second_read(lines, "consignee")[0] == normalize_party("REAL CONSIGNEE LTD")  # not the notify line
    assert second_read(lines, "notify_party")[0] == normalize_party("HARBOUR AGENCY")


def test_a_confirmed_defect_survives_a_wide_layout_and_a_name_containing_bl():
    bl = BL.replace("Consignee: MOORIM SP CO., LTD", "Consignee:            NORTHWIND B/L TRADING   GMBH        Booking No: 1")
    r = check(bl=bl)
    assert r.status == "MISMATCH" and r.defect_fields == ["consignee"]


def test_confirmed_defect_is_kept_when_another_field_needs_review():
    r = check(bl=BL.replace("Consignee: MOORIM SP CO., LTD", "Consignee: NORTHWIND TRADING GMBH").replace("Gross Weight (KGS): 21,577 KGS", "Gross Weight (KGS): N/A"))
    assert r.status == "NEEDS_REVIEW" and r.review_reason == "missing_value"
    assert r.has_defect and r.defect_fields == ["consignee"]
    sub = r.to_submission()
    assert sub["has_defect"] is True and sub["defect_fields"] == ["consignee"]


def test_reason_for_an_unconfirmed_value_is_one_of_the_four_allowed():
    from sdv.models import REVIEW_PRIORITY

    r = check(bl=BL.replace("Gross Weight (KGS): 21,577 KGS", "Gross Weight (KGS): 21,578 KGS"))
    assert r.status == "MISMATCH"  # sanity: a plain confirmed weight defect
    assert r.review_reason is None or r.review_reason in REVIEW_PRIORITY


def test_triage_is_not_brittle():
    base = {"email_id": "e", "subject": "OC 1"}
    assert rule_triage({**base, "body": "Please send the draft B/L for checking", "attachments": []}).send_draft
    t = rule_triage({**base, "body": "Attached are the SI and BL for OC 1", "attachments": ["a_SI.pdf", "a_BL.pdf"]})
    assert t.category == "BL_COMPARISON"
    sig = rule_triage({**base, "body": "Please check the draft BL against the SI.\nRegards\nhttps://agent.com/track", "attachments": []})
    assert sig.category == "BL_COMPARISON"
    assert rule_triage({**base, "body": "click https://x.co now to claim your prize", "attachments": []}).category == "SPAM"


def test_db_in_a_new_folder_and_submission_written_first(tmp_path):
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "email_001.json").write_text(json.dumps({"email_id": "email_001", "subject": "Happy new year", "body": "Happy new year!", "attachments": []}))
    from sdv.cli import main

    out = tmp_path / "out"
    assert main(["run", "--data", str(tmp_path / "data"), "--out", str(out), "--jev", "off", "--vision", "off", "--db", str(tmp_path / "new" / "deep" / "x.db")]) in (0, None)
    assert (out / "submission.json").exists() and (tmp_path / "new" / "deep" / "x.db").exists()
    # even if the store cannot be opened, the submission is already on disk
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    out2 = tmp_path / "out2"
    main(["run", "--data", str(tmp_path / "data"), "--out", str(out2), "--jev", "off", "--vision", "off", "--db", str(blocker / "x.db")])
    assert (out2 / "submission.json").exists()


HEADER = "SHIPPER / EXPORTER          CONSIGNEE               NOTIFY PARTY\n"


def test_a_row_of_bare_labels_is_a_header_not_a_value():
    bl = BL.replace("Shipper: APRIL", HEADER + "Shipper: APRIL", 1)
    r = check(bl=bl)
    assert r.status == "OK", (r.status, r.defect_fields)
    r = check(bl=bl.replace("Consignee: MOORIM SP CO., LTD", "Consignee: NORTHWIND TRADING GMBH"))
    assert r.status == "MISMATCH" and r.defect_fields == ["consignee"]
    assert extract_fields(HEADER.split("\n"), "d")["shipper"].found is False


def test_second_path_does_not_confirm_when_a_field_has_two_different_values():
    from sdv.verify import confirm

    lines_si = ["Shipper: ACME LTD"]
    lines_bl = ["SHIPPER    CONSIGNEE   FOO", "Shipper: REAL SHIPPER LTD"]  # unknown third column: not a recognisable header
    first = extract_fields(lines_bl, "d")["shipper"].value
    assert first != normalize_party("REAL SHIPPER LTD")  # the first path took the header cell
    ok, why = confirm(lines_si, lines_bl, "shipper", normalize_party("ACME LTD"), first)
    assert not ok and "more than one value" in why
    r = check(bl=BL.replace("Shipper: APRIL", "SHIPPER    CONSIGNEE   FOO\nShipper: APRIL", 1))
    assert r.status != "MISMATCH" or "shipper" not in r.defect_fields  # a wrong-line read is never an accusation


def test_failed_comparison_keeps_its_category_and_is_handed_to_a_person():
    class Down:
        def read_bytes(self, p):
            raise ConnectionResetError("boom")

    email = {"email_id": "e9", "subject": "Please check", "body": "Please check the draft BL against the SI", "attachments": ["a_SI.txt", "a_BL.txt"]}
    r = _safe_process(email, Down(), None, "off", None)
    assert r.error and r.category == "BL_COMPARISON" and r.status == "NEEDS_REVIEW" and r.review_reason == "unreadable"
    sub = r.to_submission()
    assert sub["category"] == "BL_COMPARISON" and sub["status"] == "NEEDS_REVIEW"
    plain = _safe_process({"email_id": "e10", "subject": "hi", "body": "Happy new year", "attachments": []}, Down(), None, "off", None)
    assert plain.category == "GENERAL" and plain.status == "OK" and not plain.error


def test_reports_name_a_confirmed_defect_on_a_needs_review_row():
    from sdv.report import html_report, text_report

    r = check(bl=BL.replace("Consignee: MOORIM SP CO., LTD", "Consignee: NORTHWIND TRADING GMBH").replace("Gross Weight (KGS): 21,577 KGS", "Gross Weight (KGS): N/A"))
    r.category = "BL_COMPARISON"
    assert "CONFIRMED MISMATCH" in text_report([r]) and "Confirmed mismatch" in html_report([r])


def test_upload_ids_are_unique_across_threads(tmp_path):
    import threading

    s = CaseStore(str(tmp_path / "u.db"))
    got, lock = [], threading.Lock()

    def take():
        i = s.next_upload_id()
        with lock:
            got.append(i)

    ts = [threading.Thread(target=take) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(got)) == 16


def test_title_below_a_letterhead_and_invoice_no_field_in_a_bl():
    from sdv.doctype import detect_doc_type

    lines = ["REF X"] * 9 + ["BILL OF LADING (DRAFT)", "Invoice No. 55", "Shipper: A"]
    assert detect_doc_type(lines) == "BL"
    assert detect_doc_type(["Ref"] * 9 + ["THIS IS A COMMERCIAL INVOICE"]) == "COMMERCIAL_INVOICE"
    assert detect_doc_type(["Ref"] * 3 + ["Invoice No: 5"]) == "COMMERCIAL_INVOICE"


def test_placeholder_question_marks_only_when_nothing_else_is_there():
    from sdv.normalize import is_blank

    assert is_blank("???") and is_blank("? TBC") and not is_blank("??? LTD")


def test_retry_failed_survives_another_failure_and_passes_vision(tmp_path):
    from sdv.store import retry_failed

    class Down:
        def emails(self):
            return [{"email_id": "e1", "subject": "s", "body": "Please check the draft BL against the SI", "attachments": ["a_SI.txt", "a_BL.txt"]}]

        def read_bytes(self, p):
            raise ConnectionResetError("still down")

    s = CaseStore(str(tmp_path / "r.db"))
    s.upsert(_safe_process(Down().emails()[0], Down(), None, "off", None).to_dict())
    assert retry_failed(s, Down(), None, "off", vision=None)["still_failing"] == 1


def test_console_host_option_and_request_cap(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from sdv.cli import main  # noqa: F401  (imports cleanly with the new --host option)
    from sdv.console import make_handler

    s = CaseStore(str(tmp_path / "h.db"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/check", data=b"{}",
                                 headers={"content-length": "80000000"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("expected 413")
    except (urllib.error.HTTPError, ConnectionError, OSError) as e:
        assert getattr(e, "code", 413) == 413
    srv.shutdown()


def test_jev_cache_dir_can_come_from_the_environment(tmp_path, monkeypatch):
    from sdv.jev import JevClient

    monkeypatch.setenv("SDV_JEV_CACHE", str(tmp_path / "shipped"))
    assert str(JevClient(api_key="").cache_dir) == str(tmp_path / "shipped")


def test_console_can_process_the_inbox_with_live_progress(tmp_path):
    import time

    from sdv.inbox import Inbox
    from sdv.runner import Runner

    d = tmp_path / "data" / "inbox"
    d.mkdir(parents=True)
    for i in range(6):
        (d / f"email_{i:03d}.json").write_text(json.dumps({"email_id": f"email_{i:03d}", "subject": "Happy new year", "body": "Happy new year!", "attachments": []}))
    s = CaseStore(str(tmp_path / "r.db"))
    s.upsert({"email_id": "stale_1", "subject": "old", "category": "GENERAL", "status": "OK", "comparisons": [], "evidence": {}})
    s.upsert({"email_id": "upload_001", "subject": "kept", "category": "BL_COMPARISON", "status": "OK", "comparisons": [], "evidence": {}})
    r = Runner(s, Inbox(str(tmp_path / "data")), None, "off", None)
    assert r.start(fresh=True) is True
    for _ in range(100):
        if not r.status()["running"]:
            break
        time.sleep(0.05)
    st = r.status()
    assert not st["running"] and st["done"] == 6 and st["total"] == 6 and not st["error"]
    ids = {c["email_id"] for c in s.list()}
    assert "stale_1" not in ids and "upload_001" in ids and len(ids) == 7
