from sdv.compare import compare_field, edit_distance
from sdv.models import DocRecord, ExtractedField
from sdv.normalize import is_blank, normalize, normalize_containers, normalize_party, normalize_port, normalize_weight_kg


def test_party_ignores_case_punctuation_and_legal_suffix():
    assert normalize_party("KTP CO., LTD") == normalize_party("KTP Co Ltd")
    assert normalize_party("UAE FZ-LLC") == normalize_party("uae fz llc")
    assert normalize_party("BALL & DOGGETT AUSTRALIA PTY LTD") == normalize_party("Ball and Doggett Australia Pty Ltd")


def test_party_different_word_is_a_real_difference():
    assert normalize_party("EAST BRIGHT FZ-LLC") != normalize_party("UAB NOVAKOPA")
    assert normalize_party("APRIL FINE PAPER TRADING") != normalize_party("APRIL FINE PAPER TRADING (MIDDLE EAST) FZE")


def test_port_drops_locode_and_terminal_but_keeps_name():
    assert normalize_port("SINGAPORE (SGSIN)") == normalize_port("SINGAPORE")
    assert normalize_port("PORT KLANG (WESTPORT), MALAYSIA") == normalize_port("PORT KLANG, MALAYSIA")
    # A changed port that keeps the ORIGINAL code must still differ (comparing codes would miss it).
    assert normalize_port("MOMBASA, KENYA (KEMBA)") != normalize_port("TUTICORIN, INDIA (KEMBA)")


def test_containers_and_weight():
    assert normalize_containers("15 x 20'GP") == normalize_containers("15 x 20'FCL") == 15
    assert normalize_weight_kg("22,000.00 KGS") == normalize_weight_kg("22000") == 22000.0
    assert normalize_weight_kg("22 MT") == 22000.0


def test_placeholders():
    for v in ["", "N/A", "TBA", "____MT", "_______ MTS", "???", "TBD", "-"]:
        assert is_blank(v), v
    assert not is_blank("6 x 40'HC")


def _rec(field, raw, provenance="native", confidence=1.0):
    ef = ExtractedField(field=field, raw=raw, found=True, provenance=provenance, confidence=confidence, value=normalize(field, raw))
    d = DocRecord(name="x")
    d.fields[field] = ef
    return d


def test_numbers_are_exact_never_tolerated():
    c = compare_field("container_count", _rec("container_count", "3 x 40'HC"), _rec("container_count", "4 x 40'HC"))
    assert c.verdict == "mismatch"
    c = compare_field("gross_weight_kg", _rec("gross_weight_kg", "22,000 KG"), _rec("gross_weight_kg", "22,500 KG"))
    assert c.verdict == "mismatch"


def test_native_text_difference_of_one_letter_is_a_real_mismatch():
    c = compare_field("port_of_discharge", _rec("port_of_discharge", "TUTICORIN, INDIA"), _rec("port_of_discharge", "TUTICOPIN, INDIA"))
    assert c.verdict == "mismatch"


def test_ocr_one_letter_difference_is_uncertain_not_accused():
    c = compare_field("port_of_discharge", _rec("port_of_discharge", "TUTICORIN, INDIA"), _rec("port_of_discharge", "TUTICOPIN, INDIA", provenance="ocr"))
    assert c.verdict == "uncertain"


def test_blank_is_uncertainty_not_a_discrepancy():
    si = _rec("gross_weight_kg", "N/A")
    si.fields["gross_weight_kg"].blank = True
    c = compare_field("gross_weight_kg", si, _rec("gross_weight_kg", "235,550 KG"))
    assert c.verdict == "uncertain" and "blank" in c.reason


def test_edit_distance():
    assert edit_distance("AL GURG", "AL GUAG") == 1
    assert edit_distance("a", "a") == 0


def test_number_formats_are_read_as_written_in_that_convention():
    from sdv.normalize import normalize_weight_kg as w

    for raw in ["21,577 KG", "21.577,00 KG", "21 577 KG", "21\u00a0577 kg", "21'577 KG", "21577", "21577.00 KGS", "21.577 KG", "21,577.00 KGS", "21.577 MT", "21,577 kgs"]:
        assert w(raw) == 21577.0, raw
    assert w("21577,50 KG") == 21577.5 and w("0.500 KG") == 0.5 and w("____ MT") is None


def test_scan_reading_noise_is_uncertain_not_mismatch_but_native_text_stays_strict():
    from sdv.compare import compare_field
    from sdv.models import DocRecord, ExtractedField
    from sdv.normalize import normalize

    def rec(field, raw, prov):
        return DocRecord(name="x", fields={field: ExtractedField(field=field, value=normalize(field, raw), raw=raw, found=True, provenance=prov)})

    # the same weight written with a decimal point instead of a comma is now the same number
    assert compare_field("gross_weight_kg", rec("gross_weight_kg", "128,544 KG", "vision"), rec("gross_weight_kg", "128.544 KG", "vision")).verdict == "match"
    assert compare_field("port_of_loading", rec("port_of_loading", "NHA VA SHIEVA INDIA", "vision"), rec("port_of_loading", "INHAVA SHEVA, INDIA", "vision")).verdict == "uncertain"
    # a genuinely different weight in native text is still a mismatch
    assert compare_field("gross_weight_kg", rec("gross_weight_kg", "128,544 KG", "native"), rec("gross_weight_kg", "128,545 KG", "native")).verdict == "mismatch"
    # scans do not hide a genuinely different value
    assert compare_field("consignee", rec("consignee", "ACME TRADING LLC", "vision"), rec("consignee", "ZEBRA LOGISTICS PTE", "vision")).verdict == "mismatch"
