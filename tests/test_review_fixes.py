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
