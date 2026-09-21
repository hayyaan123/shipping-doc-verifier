"""Integration test over the hackathon bundle. Skipped unless SDV_DATA points at the extracted bundle."""
import os
from collections import Counter

import pytest

from sdv.inbox import Inbox
from sdv.models import CATEGORIES
from sdv.pipeline import process_all

DATA = os.environ.get("SDV_DATA")
pytestmark = pytest.mark.skipif(not DATA or not os.path.isdir(DATA), reason="set SDV_DATA to the extracted bundle folder")


def test_every_email_gets_a_valid_result_and_nothing_crashes():
    results = process_all(Inbox(DATA), None, "off")
    assert len(results) == 520
    assert not [r for r in results if r.error]
    assert all(r.category in CATEGORIES for r in results)
    cmp_ = [r for r in results if r.category == "BL_COMPARISON"]
    for r in cmp_:
        if r.status == "NEEDS_REVIEW":
            assert r.review_reason in ("unreadable", "wrong_doc_type", "missing_attachment", "missing_value")
        if r.status == "MISMATCH":
            assert r.defect_fields and r.has_defect
        if r.status == "OK":
            assert not r.defect_fields


def test_runs_are_reproducible():
    a = process_all(Inbox(DATA), None, "off")
    b = process_all(Inbox(DATA), None, "off")
    assert [x.to_submission() for x in a] == [x.to_submission() for x in b]
