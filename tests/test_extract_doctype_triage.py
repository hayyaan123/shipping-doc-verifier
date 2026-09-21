from sdv.doctype import detect_doc_type
from sdv.extract import extract_fields
from sdv.readers import read_text_bytes
from sdv.triage import clean_body, rule_triage


def fields(text):
    return extract_fields(read_text_bytes(text.encode()).lines, "t.txt")


def test_label_synonyms_map_to_the_same_field():
    a = fields("Shipper: A\nTo the Order of: B\nNotify Party/Intermediate Consignee: C\nLoad Port: D, X\nPOD: E, Y\nTotal Containers: 3 x 20'GP\nGross Wt (kgs): 1,000 KG")
    b = fields("SHIPPER/EXPORTER: A\nConsignee (Non-Negotiable): B\nNotify: C\nPort of Loading (POL): D, X\nPort of Discharge (POD): E, Y\nNo. of Containers or Packages: 3 x 20'GP\nGross Weight毛重(KGS): 1000")
    for f in a:
        assert a[f].found and b[f].found, f
        assert a[f].value == b[f].value, f


def test_party_value_is_the_name_not_the_address():
    f = fields("Shipper: APRIL FINE PAPER TRADING\n  ON BEHALF OF X; 77 ROBINSON ROAD\nConsignee: Z")
    assert f["shipper"].raw == "APRIL FINE PAPER TRADING"


def test_container_table_header_is_not_a_container_count():
    f = fields("CONTAINER NO.    DESCRIPTION    GROSS WEIGHT (KG)\nABCD1234567   40'HC PAPER   21,887\nNo. of Containers: 6 x 40'HC\nTOTAL Gross Wt (kgs): 131,322 KG")
    assert f["container_count"].value == 6
    assert f["gross_weight_kg"].value == 131322.0


def test_placeholders_and_blanks_are_flagged_not_compared():
    f = fields("SHIPPER: \nCONSIGNEE: UAB X\nPORT OF DISCHARGE: N/A\nGROSS WEIGHT: ____MT")
    assert f["shipper"].blank and f["port_of_discharge"].blank and f["gross_weight_kg"].blank
    assert not f["consignee"].blank


def test_doctype_from_content_not_filename():
    assert detect_doc_type(["COMMERCIAL INVOICE", "====", "Invoice No.: 1"]) == "COMMERCIAL_INVOICE"
    assert detect_doc_type(["PACKING LIST"]) == "PACKING_LIST"
    assert detect_doc_type(["CERTIFICATE OF ORIGIN"]) == "CERTIFICATE_OF_ORIGIN"
    # instruction wording is tested before bill-of-lading wording
    assert detect_doc_type(["BILL OF LADING INSTRUCTION", "B/L NUMBER: X"]) == "SI"
    assert detect_doc_type(["SHIPPING INSTRUCTION"]) == "SI"
    assert detect_doc_type(["BILL OF LADING (DRAFT)"]) == "BL"
    assert detect_doc_type(["[sheet] S.I.", "APRIL", "BL INSTRUCTION: 1"]) == "SI"
    assert detect_doc_type(["[sheet] BL", "APRIL", "BILL OF LADING: 1"]) == "BL"


def mail(subject, body, atts=()):
    return {"email_id": "e", "subject": subject, "body": body, "attachments": list(atts)}


def test_triage_banner_and_intent():
    banner = "WARNING: This email originated outside of our organisation. As a security measure, please exercise caution.\n\n"
    t = rule_triage(mail("x", banner + "Dear Ooi,\n\nPlease find attached the shipping instruction and the draft bill of lading for I1 for your confirmation. Kindly verify the BL matches the SI", ["attachments/e_SI.txt", "attachments/e_BL.txt"]))
    assert t.category == "BL_COMPARISON" and not t.send_draft
    assert clean_body(banner + "Hi").strip() == "Hi"
    t = rule_triage(mail("x", "Dear Ooi, Please assist to send the draft BL for I1 for checking asap. Thank you."))
    assert t.category == "BL_COMPARISON" and t.send_draft


def test_si_request_is_not_a_comparison_even_if_it_mentions_the_draft_bl():
    t = rule_triage(mail("REQUEST SI _ 5RFR", "Please find Shipping instruction for 5RFR.\n\nPlease revert with draft BL once available."))
    assert t.category == "SI_REQUEST"


def test_misleading_subject_does_not_decide_the_category():
    t = rule_triage(mail("_RPA_ India HSS SD Billing Process Completed", "Wishing everyone a happy and prosperous New Year 2026!"))
    assert t.category == "GENERAL"
    t = rule_triage(mail("REQUEST BL DRAFT _ PO 1", "Attached are the SI and draft BL for OC 1. Please check the details and confirm.", ["attachments/e_SI.txt", "attachments/e_BL.txt"]))
    assert t.category == "BL_COMPARISON"


def test_spam_and_invoice():
    assert rule_triage(mail("WIN", "You have won a brand new iPhone! Claim at http://free.example")).category == "SPAM"
    assert rule_triage(mail("q", "Hi, Query on invoice 123: is the THC billed separately?")).category == "INVOICE_QUERY"
