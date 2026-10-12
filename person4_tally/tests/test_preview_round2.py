"""Purchase preview, round-2 review findings: unknown nested keys and P3's extra draft key, keys of mixed types,
invisible bidi / byte-order-mark characters, a company GUID compared exactly with every unresolved ledger reported,
and narration whitespace refused instead of silently stripped. Pure: nothing here talks to any Tally.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

import pytest

import finledger_tally.voucher_xml as voucher_xml
from finledger_tally import DraftRejected, LedgerMasters
from finledger_tally.mapping import LedgerResolution, LedgerStatus, MappingTable
from test_preview_hardening import GOLDEN_SHA256, _p3_boundary, _place, codes_of, line, p3_job
from test_voucher_xml import COMPANY, DOC, GOLDEN, MASTERS, PURCHASE, job, preview

from contracts.schemas.voucher_draft import VoucherDraft


def reasons_of(j=None, **kw) -> tuple[tuple[str, str], ...]:
    with pytest.raises(DraftRejected) as info:
        preview(j, **kw)
    return info.value.reasons


# ====================== 1. unknown keys in every nested object; P3's own extra key ======================


@pytest.mark.parametrize("key,value", [
    ("gst", {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {}, "rate": 18}),
    ("tds", {"applicable": False, "section": None, "ledger": None, "amount": "0", "tds_rate": "2"}),
    ("bill_wise", {"ref_type": "new_ref", "ref": "TEST-INV-001", "due_date": None, "against": "INV-1"}),
    ("lines", [line(PURCHASE, "1000.00"), {**line(PURCHASE, "180.00"), "cgst_rate": "9"}]),  # not only lines[0]
])
def test_an_unknown_key_in_any_nested_object_is_refused(key, value):
    reasons = reasons_of(job(**{key: value}))
    assert any(code == "unsupported_field" for code, _ in reasons)


def test_p3_sends_exactly_one_key_outside_the_shared_contract_and_forbids_nested_extras():
    boundary = _p3_boundary()
    assert set(boundary.VoucherDraftBoundary.model_fields) - set(VoucherDraft.model_fields) == {"ai_paragraph"}
    for model in (boundary.VoucherLine, boundary.GstDraft, boundary.TdsDraft, boundary.BillWise):
        assert model.model_config.get("extra") == "forbid"  # P3 cannot hand over a nested extra at all


@pytest.mark.parametrize("value", [{"tax_rate": 18}, ["x"], 5, True, 1.5])
def test_an_ai_paragraph_that_is_not_text_is_refused(value):
    assert "invalid_contract" in codes_of(job(ai_paragraph=value))


def test_an_ai_paragraph_is_accepted_reported_as_not_written_and_never_written():
    p = preview(job(ai_paragraph="Looks fine <b>18% GST</b>"))
    assert p.xml == GOLDEN.read_bytes() and b"18% GST" not in p.xml
    assert p.not_written == ("ai_paragraph (P3 reviewer summary; never sent to Tally)",)
    assert preview(p3_job()).not_written == ()  # P3's null ai_paragraph: nothing to report


# ====================== 2. keys of mixed types ======================


def _mixed(where: str) -> dict:
    j = job()
    target = {"job": j, "draft": j["voucher_draft"], "gst": j["voucher_draft"]["gst"],
              "tds": j["voucher_draft"]["tds"], "bill_wise": j["voucher_draft"]["bill_wise"],
              "line": j["voucher_draft"]["lines"][0]}[where]
    target.update({1: 2, "x": 3, None: 4})
    return j


@pytest.mark.parametrize("where", ["job", "draft", "gst", "tds", "bill_wise", "line"])
def test_unknown_keys_of_mixed_types_are_refused_never_a_type_error(where):
    reasons = reasons_of(_mixed(where))
    assert {code for code, _ in reasons} & {"invalid_job", "unsupported_field"}
    assert any("'x'" in msg and "1" in msg and "None" in msg for _, msg in reasons)


def test_a_missing_job_field_with_mixed_unknown_keys_is_refused():
    j = _mixed("job")
    del j["revision"]
    assert codes_of(j) == {"invalid_job"}


# ====================== 3. characters XML cannot carry; bidi controls and U+FEFF ======================

_DISGUISE = ["‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎",
             "‏", "؜", "﻿"]
_XML_ILLEGAL = ["￾", "￿", "\ud800", "\udbff", "\udc00", "\udfff"]


@pytest.mark.parametrize("char", _DISGUISE + _XML_ILLEGAL, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("field,at", [("reference", "middle"), ("reference", "end"), ("narration", "middle"),
                                      ("narration", "start"), ("party", "middle"), ("line_ledger", "middle"),
                                      ("mapped_ledger", "middle")])
def test_invisible_and_xml_illegal_characters_are_refused_as_invalid_text(field, at, char):
    text = {"middle": f"INV{char}001", "end": f"INV-001{char}", "start": f"{char}INV-001"}[at]
    if field == "mapped_ledger":  # the name a mapping row hands back is what gets written, so it is checked too
        masters = LedgerMasters(COMPANY.guid, MASTERS.ledgers | {text})
        mapping = MappingTable.from_rows([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": text}])
        j, kw = job(lines=[line("Purchase - RM", "1180.00")]), {"masters": masters, "mapping": mapping}
    else:
        j, kw = _place(field, text)
    reasons = reasons_of(j, **kw)
    assert "invalid_text" in {code for code, _ in reasons}


@pytest.mark.parametrize("text", ["क्‍ष", "न‌हीं", "A B", "عربي"])
def test_joiners_and_inner_no_break_spaces_stay_allowed(text):
    p = preview(job(narration=text))
    assert ET.fromstring(p.xml).findtext(".//NARRATION") == f"{text} finledger:document_id={DOC}"


# ====================== 4. company GUID compared exactly; every unresolved ledger reported ======================


def test_unknown_ledgers_are_refused_when_masters_differ_from_the_company_guid_only_by_case():
    ghost = "Ghost Vendor"
    masters = LedgerMasters(COMPANY.guid.upper(), MASTERS.ledgers | {ghost, "Ghost Purchase"})
    reasons = reasons_of(job(party_ledger=ghost, lines=[line("Ghost Purchase", "1180.00")]), masters=masters)
    assert ("company_mismatch", "ledger masters were not fetched from the verified company") in reasons
    ledger_problems = {msg.split(":")[0] for code, msg in reasons if code == "company_mismatch" and ":" in msg}
    assert ledger_problems == {"party ledger", "lines[0]"}


@pytest.mark.parametrize("status,code", [(LedgerStatus.COMPANY_UNVERIFIED, "company_mismatch"),
                                         (LedgerStatus.MASTERS_UNAVAILABLE, "masters_unavailable"),
                                         (LedgerStatus.MISSING, "missing_ledger"),
                                         (LedgerStatus.AMBIGUOUS, "ambiguous_ledger")])
def test_resolution_always_records_a_problem_for_a_non_exists_status(status, code):
    problems = [(code, "already recorded elsewhere")]
    result = voucher_xml._resolution(LedgerResolution("X", status, None, "detail"), "lines[3]", problems)
    assert result is None and problems[-1] == (code, "lines[3]: detail") and len(problems) == 2


# ====================== 5. narration whitespace: refused, never silently stripped ======================


@pytest.mark.parametrize("ws", [" ", "　", " ", " ", " ", " "],
                         ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("where", ["start", "end", "alone", "inside_spaces"])
def test_unicode_whitespace_at_the_ends_of_the_narration_is_refused(ws, where):
    narration = {"start": f"{ws}Test", "end": f"Test{ws}", "alone": ws, "inside_spaces": f"  {ws}Test  "}[where]
    reasons = reasons_of(job(narration=narration))
    assert [msg for code, msg in reasons if code == "invalid_text" and msg.startswith("narration")]


def test_ascii_spaces_around_the_narration_are_still_dropped_and_goldens_unchanged():
    assert preview(job(narration="  Test purchase  ")).xml == GOLDEN.read_bytes()
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256
