"""Purchase preview hardening (mission Part 9): the shared VoucherDraft and P3's boundary output, company identity,
reference, date, ledgers, bill-wise, exact amounts, signs, escaping, Unicode, marker, digest and determinism.

Everything here is pure: no test talks to any Tally, real or fake, and one test proves the preview cannot open a
socket. Goldens are files in fixtures/preview/; the two older ones are left byte-identical.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from decimal import ROUND_FLOOR, Decimal, Inexact, Overflow, Rounded, getcontext, localcontext
from pathlib import Path

import pytest

import finledger_tally.voucher_xml as voucher_xml
from finledger_tally import DraftRejected, LedgerMasters, TallyCompany, TallyTransport, VoucherPreview
from finledger_tally.identity import _EXACT_MARKER
from finledger_tally.mapping import LedgerResolution, LedgerStatus, MappingTable
from finledger_tally.transport import assert_voucher_import
from test_voucher_xml import COMPANY, DOC, GOLDEN, MASTERS, PURCHASE, VENDOR, entries, job, preview

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
PREVIEW_DIR = HERE / "fixtures" / "preview"
MULTILINE = PREVIEW_DIR / "purchase_exempt_multiline_1180.xml"
UNICODE_GOLDEN = PREVIEW_DIR / "purchase_exempt_unicode_escaped_1180.xml"
UNICODE_SHA256 = "2ab015ee5bbff975d96dee317375d837ce728174f1105ce9ab8c12d17f186be8"
GOLDEN_SHA256 = "d9e312e455c5b290ae281d816640685ebf78cbb013989032695ff4a14de58dd3"
RND, DISCOUNT = "R&D <Supplies>", "Discount Received"
TEMPLATE_TAGS = {
    "ENVELOPE", "HEADER", "TALLYREQUEST", "BODY", "IMPORTDATA", "REQUESTDESC", "REPORTNAME", "STATICVARIABLES",
    "SVCURRENTCOMPANY", "REQUESTDATA", "TALLYMESSAGE", "VOUCHER", "DATE", "VOUCHERTYPENAME", "REFERENCE",
    "PARTYLEDGERNAME", "NARRATION", "ISINVOICE", "ALLLEDGERENTRIES.LIST", "LEDGERNAME", "ISDEEMEDPOSITIVE",
    "ISPARTYLEDGER", "AMOUNT", "BILLALLOCATIONS.LIST", "NAME", "BILLTYPE",
}


def line(ledger: str, amount, is_debit: bool = True) -> dict:
    return {"ledger": ledger, "amount": amount, "is_debit": is_debit, "cost_centre": None}


MULTI_LINES = [line(PURCHASE, "1000.00"), line(RND, "200.00"), line(DISCOUNT, "20.00", False)]


def _id(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 10 ** 100:
        return "int_5000_digits"
    return repr(value)[:24]


def codes_of(j=None, **kw) -> set[str]:
    """The problem codes of a refused preview; fails the test if XML came back or anything else was raised."""
    with pytest.raises(DraftRejected) as info:
        preview(j, **kw)
    assert all(isinstance(c, str) and isinstance(m, str) for c, m in info.value.reasons)
    return info.value.codes


def with_doc_id(value) -> dict:
    j = job(document_id=value, attachment_document_id=value)
    j.update(document_id=value, idempotency_key=f"{value}:r1")
    return j


# ====================== shared contract and the P3 boundary ======================


def _p3_boundary():
    """Person 3's boundary module, loaded from its file (read-only) without importing the rest of its package."""
    name = "_p3_boundary_for_p4_preview_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "person3_control_ui" / "src" / "finledger_control" / "boundary.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # pydantic resolves the module's postponed annotations through sys.modules
        spec.loader.exec_module(module)
    return sys.modules[name]


def p3_job(**draft_changes) -> dict:
    """A post job whose draft went through P3's validate_draft and JSON, as P3 stores it in jobs.payload."""
    payload = job(**draft_changes)["voucher_draft"]
    dumped, _ = _p3_boundary().validate_draft(json.loads(json.dumps(payload, default=str)), DOC)
    j = job()
    j["voucher_draft"] = json.loads(json.dumps(dumped))
    return j


@pytest.mark.parametrize("amount", ["1180.00", "1180", 1180, 1180.0, "1180.000"])
def test_p3_boundary_output_previews_to_the_golden_bytes(amount):
    j = p3_job(lines=[line(PURCHASE, amount)])
    dumped_amount = j["voucher_draft"]["lines"][0]["amount"]
    assert isinstance(dumped_amount, str)  # P3 dumps Decimal amounts as JSON strings
    assert j["voucher_draft"]["tds"]["amount"] == "0" and j["voucher_draft"]["ai_paragraph"] is None
    p = preview(j, expected_total=Decimal("1180.00"))
    assert p.xml == GOLDEN.read_bytes() and p.sha256 == GOLDEN_SHA256


def test_p3_boundary_multi_line_output_previews_to_its_golden_bytes():
    assert preview(p3_job(lines=MULTI_LINES), expected_total="1180.00").xml == MULTILINE.read_bytes()


@pytest.mark.parametrize("amount,dumped", [("1e3", "1E+3"), ("1.18E+3", "1.18E+3"), (Decimal("1.18E+3"), "1.18E+3")])
def test_exponent_amounts_that_p3_passes_through_verbatim_are_refused(amount, dumped):
    # Decision: plain decimal notation only. P3 keeps the producer's notation, and "1E+3" vs "1E-3" is one character.
    j = p3_job(lines=[line(PURCHASE, amount)])
    assert j["voucher_draft"]["lines"][0]["amount"] == dumped
    assert "invalid_amount" in codes_of(j)


def test_p3_journal_voucher_type_is_named_as_unsupported():
    j = p3_job(voucher_type="journal")  # valid at P3's boundary, absent from the shared enum (gap 13)
    assert j["voucher_draft"]["voucher_type"] == "journal"
    with pytest.raises(DraftRejected) as info:
        preview(j)
    assert {"unsupported_voucher_type", "invalid_contract"} <= info.value.codes
    assert "#13" in dict(info.value.reasons)["unsupported_voucher_type"]


@pytest.mark.parametrize("treatment", ["non_gst", "reverse_charge"])
def test_p3_only_gst_treatments_are_refused(treatment):
    j = p3_job(gst={"treatment": treatment, "input_ledgers": [], "tax_breakup": {}})
    assert j["voucher_draft"]["gst"]["treatment"] == treatment
    assert "invalid_contract" in codes_of(j)


@pytest.mark.parametrize("key,value", [
    ("lines", [{**line(PURCHASE, "1180.00"), "tax_rate": "18"}]),
    ("lines", [{**line(PURCHASE, "1180.00"), "igst": 180}]),
    ("gst", {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {}, "cess": "5.00"}),
    ("tds", {"applicable": False, "section": None, "ledger": None, "amount": "0", "rate": "2"}),
    ("bill_wise", {"ref_type": "new_ref", "ref": "TEST-INV-001", "due_date": None, "original_invoice_ref": "INV-9"}),
])
def test_unknown_nested_fields_are_refused_not_ignored(key, value):
    assert "unsupported_field" in codes_of(job(**{key: value}))


@pytest.mark.parametrize("key,value", [
    ("lines", (x for x in [line(PURCHASE, "1180.00")])),
    ("lines", ["not an object"]),
    ("lines", {"0": line(PURCHASE, "1180.00")}),
    ("gst", None),
    ("gst", ["exempt"]),
    ("tds", "none"),
    ("bill_wise", "TEST-INV-001"),
])
def test_nested_values_that_are_not_json_objects_are_refused(key, value):
    assert "invalid_contract" in codes_of(job(**{key: value}))


def test_contract_model_instances_inside_the_draft_are_refused_not_crashed():
    from contracts.schemas.voucher_draft import BillWise, TdsDetails, VoucherLine
    for key, value in (("lines", [VoucherLine(ledger=PURCHASE, amount=1180.0)]), ("tds", TdsDetails()),
                       ("bill_wise", BillWise(ref="TEST-INV-001"))):
        assert "invalid_contract" in codes_of(job(**{key: value}))


# ====================== company identity ======================


@pytest.mark.parametrize("masters_guid", [COMPANY.guid.upper(), f" {COMPANY.guid}", f"{COMPANY.guid}\n",
                                          "6a250654-44be-4954-a9c8-6cd5e29f4e76"])
def test_masters_of_a_differently_written_guid_never_skip_the_ledger_check(masters_guid):
    # Before the fix a GUID differing only in case or padding passed the masters check, and every ledger lookup
    # then reported company_unverified, which was dropped: a ghost ledger previewed fine.
    masters = LedgerMasters(masters_guid, MASTERS.ledgers | {"Ghost Vendor"})
    assert "company_mismatch" in codes_of(job(party_ledger="Ghost Vendor"), masters=masters)


def test_a_company_guid_is_never_normalised():
    upper = COMPANY.guid.upper()
    codes = codes_of(company=TallyCompany(COMPANY.name, upper), masters=LedgerMasters(upper, MASTERS.ledgers))
    assert "company_mismatch" in codes
    # Discriminating case (round 2): a verified lowercase company with masters whose GUID differs only in case. A
    # normalising comparison (.strip().lower()) would accept these masters; each ledger must still be refused itself.
    with pytest.raises(DraftRejected) as info:
        preview(job(party_ledger="Ghost Vendor"), masters=LedgerMasters(upper, MASTERS.ledgers | {"Ghost Vendor"}))
    assert ("company_mismatch", "ledger masters were not fetched from the verified company") in info.value.reasons
    assert [m for c, m in info.value.reasons if c == "company_mismatch" and m.startswith("party ledger:")]


@pytest.mark.parametrize("status,code", [(LedgerStatus.COMPANY_UNVERIFIED, "company_mismatch"),
                                         (LedgerStatus.MASTERS_UNAVAILABLE, "masters_unavailable"),
                                         (LedgerStatus.MISSING, "missing_ledger"),
                                         (LedgerStatus.AMBIGUOUS, "ambiguous_ledger")])
@pytest.mark.parametrize("resolver", ["resolve_ledger", "resolve_party"])
def test_an_unresolved_ledger_can_never_pass(monkeypatch, resolver, status, code):
    monkeypatch.setattr(voucher_xml, resolver, lambda name, *a, **k: LedgerResolution(name, status, None, "test"))
    assert code in codes_of()


@pytest.mark.parametrize("name", ["", " FinLedger Test Company", "FinLedger Test Company ", "FinLedger\tTest Company",
                                  "FinLedger Test Company\x00", "FinLedger\u2028Test Company"])
def test_unusable_company_names_are_refused_even_when_they_match(name):
    assert "company_mismatch" in codes_of(company=TallyCompany(name, COMPANY.guid), expected_company_name=name)


def test_company_names_are_compared_exactly():
    assert "company_mismatch" in codes_of(expected_company_name=COMPANY.name.lower())
    assert "company_mismatch" in codes_of(expected_company_name=COMPANY.name + " ")


@pytest.mark.parametrize("rows,code", [
    ([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": PURCHASE.upper()}], "ambiguous_ledger"),
    ([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": "Purchases"}], "ambiguous_ledger"),
    ([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": "Purchase - Steel"},
      {"kind": "ledger", "our_key": "Purchase - Steel", "erp_value": PURCHASE}], "missing_ledger"),
], ids=["case_variant", "alias", "chain_not_followed"])
def test_a_mapping_that_does_not_name_exactly_one_ledger_is_refused(rows, code):
    masters = LedgerMasters(COMPANY.guid, MASTERS.ledgers, aliases=(("Purchases", PURCHASE),))
    j = job(lines=[line("Purchase - RM", "1180.00")])
    assert code in codes_of(j, masters=masters, mapping=MappingTable.from_rows(rows))


# ====================== document ids ======================


@pytest.mark.parametrize("bad", [DOC.upper(), "{" + DOC + "}", "urn:uuid:" + DOC, DOC.replace("-", ""), " " + DOC,
                                 DOC[:-1], DOC[:-1] + "g", "9d4f3d5d-499e-41db-b510-e98348e5b5030"])
def test_non_canonical_document_ids_are_refused(bad):
    assert "invalid_job" in codes_of(with_doc_id(bad))


@pytest.mark.parametrize("value", [uuid.UUID(DOC), int(uuid.UUID(DOC))])
def test_a_job_document_id_that_is_not_a_string_is_refused(value):
    j = job()
    j["document_id"] = value
    assert "invalid_job" in codes_of(j)


# ====================== supplier reference, date, bill-wise ======================


def test_reference_is_written_verbatim_as_reference_and_new_ref_bill_only():
    j = job(bill_wise={"ref_type": "new_ref", "ref": "INV/26-27/0042", "due_date": "2026-05-01"})
    p = preview(j)
    root = ET.fromstring(p.xml)
    assert root.findtext(".//VOUCHER/REFERENCE") == p.reference == "INV/26-27/0042"
    party, purchase = entries(p.xml)
    [bill] = party.findall("BILLALLOCATIONS.LIST")
    assert (bill.findtext("NAME"), bill.findtext("BILLTYPE"), bill.findtext("AMOUNT")) == ("INV/26-27/0042", "New Ref",
                                                                                          "1180.00")
    assert purchase.find("BILLALLOCATIONS.LIST") is None
    assert {e.tag for e in root.iter()} == TEMPLATE_TAGS  # no due date, credit period or bill date is invented
    assert p.not_written == ("bill_wise.due_date (no verified Tally field for it yet)",)


@pytest.mark.parametrize("ref_type", ["against_ref", "advance", "on_account"])
def test_bill_wise_kinds_other_than_new_ref_are_refused(ref_type):
    assert "unsupported_bill_wise" in codes_of(job(bill_wise={"ref_type": ref_type, "ref": "TEST-INV-001"}))


@pytest.mark.parametrize("ref", ["", " ", "\t"])
def test_a_blank_reference_is_refused(ref):
    assert "missing_reference" in codes_of(job(bill_wise={"ref_type": "new_ref", "ref": ref}))


@pytest.mark.parametrize("d", ["2026-W14-3", "2026-W14", "2026-091", "20260401", "2026-04-01Z", "2026-04-01T00:00",
                               "2026-04-01 00:00:00", "2026-04-01+05:30", "２０２６-０４-０１", "2026-4-01", "2026-04-1",
                               "0000-01-01", "2026-02-29", "2026-04-31", "2026-04-01\n", "01/04/2026", "1 Apr 2026"])
def test_dates_that_are_not_real_yyyy_mm_dd_are_refused(d):
    # 2026-W14-3 is 10 characters and date.fromisoformat reads it as 2026-04-01: it was accepted before the fix.
    assert codes_of(job(date=d)) == {"invalid_date"}


@pytest.mark.parametrize("d", [20260401, None, ["2026-04-01"]])
def test_dates_that_are_not_strings_are_refused(d):
    assert "invalid_contract" in codes_of(job(date=d))


@pytest.mark.parametrize("d,written", [("2026-04-01", "20260401"), ("2028-02-29", "20280229"),
                                       ("2026-12-31", "20261231"), ("2027-01-01", "20270101")])
def test_valid_dates_are_written_as_yyyymmdd(d, written):
    p = preview(job(date=d))
    assert ET.fromstring(p.xml).findtext(".//VOUCHER/DATE") == p.voucher_date == written


# ====================== amounts: exact Decimals, never rounded ======================


@pytest.mark.parametrize("amount", [
    "1e3", "1E+3", "1.18E+3", "1180e0", "+1180", ".5", "1180.", "1_180.00", "\t1180", "1180\n", "\u00a01180",
    "NaN", "nan", "Infinity", "-Infinity", "inf", True, False, "0", "0.00", "-0", "-1180.00", "1180.001",
    "1180.0001", Decimal("1.18E+3"), Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf"), 1e-05, 1e16,
    "1000000000000", "1000000000000.00", "9" * 5000, 10 ** 13,
], ids=_id)
def test_invalid_line_amounts_are_refused_as_invalid_amount(amount):
    assert "invalid_amount" in codes_of(job(lines=[line(PURCHASE, amount)]))


@pytest.mark.parametrize("amount", [None, "", " ", "1,180.00", "0x10", "₹1180", "1180.00 INR", "١١٨٠", [1180],
                                    {"v": 1}, Decimal("sNaN"), 10 ** 5000], ids=_id)
def test_amounts_the_shared_contract_cannot_read_are_refused_as_invalid_contract(amount):
    assert "invalid_contract" in codes_of(job(lines=[line(PURCHASE, amount)]))


@pytest.mark.parametrize("amount,exact", [("0.1", "0.10"), ("1", "1.00"), ("1180.000", "1180.00"), ("007.50", "7.50"),
                                          (999999999999.99, "999999999999.99"), ("999999999999.99", "999999999999.99")])
def test_valid_amounts_are_exact_decimals_with_two_places(amount, exact):
    p = preview(job(lines=[line(PURCHASE, amount)]))
    assert isinstance(p.party_amount, Decimal) and p.party_amount.as_tuple().exponent == -2
    assert str(p.party_amount) == exact and [str(a) for _, a, _ in p.lines] == [exact]
    assert [e.findtext("AMOUNT") for e in entries(p.xml)] == [exact, f"-{exact}"]


def test_a_party_amount_above_numeric_14_2_is_refused():
    lines = [line(PURCHASE, "999999999999.99"), line(RND, "0.01")]
    assert "invalid_amount" in codes_of(job(lines=lines))


@pytest.mark.parametrize("setup", [
    lambda c: setattr(c, "prec", 6),
    lambda c: (setattr(c, "prec", 3), setattr(c, "rounding", ROUND_FLOOR)),
    lambda c: c.traps.update({Inexact: True, Rounded: True}),
    lambda c: (setattr(c, "Emax", 3), c.traps.update({Overflow: True})),
], ids=["prec6", "prec3_floor", "trap_inexact_rounded", "emax3"])
def test_the_callers_decimal_context_cannot_round_or_break_the_amounts(setup):
    # Before the fix, with prec=6, 9999.99 + 0.02 became a party amount of 10000.0 under lines summing to 10000.01.
    lines = [line(PURCHASE, "9999.99"), line(RND, "0.02"), line(DISCOUNT, "0.01", False)]
    expected = preview(job(lines=lines))
    with localcontext() as ctx:
        setup(ctx)
        p = preview(job(lines=lines), expected_total="10000.00")
        assert getcontext() is ctx  # the caller's context is left as it was
    assert p.xml == expected.xml and p.party_amount == Decimal("10000.00")
    assert [e.findtext("AMOUNT") for e in entries(p.xml)] == ["10000.00", "-9999.99", "-0.02", "0.01"]


@pytest.mark.parametrize("total", ["1180.00", "1180", 1180, 1180.0, Decimal("1180.0"), Decimal("1180"), "1180.000"],
                         ids=_id)
def test_an_expected_total_equal_to_the_party_amount_is_accepted_and_not_written(total):
    assert preview(expected_total=total).xml == GOLDEN.read_bytes()


@pytest.mark.parametrize("total", ["1e3", "1.18E+3", Decimal("1.18E+3"), "1180.005", 0.1 + 0.2, True, "NaN",
                                   Decimal("sNaN"), Decimal("Infinity"), float("inf"), "", "1,180.00", [1180],
                                   10 ** 5000, "9" * 100], ids=_id)
def test_a_malformed_expected_total_is_refused(total):
    assert "invalid_amount" in codes_of(expected_total=total)


@pytest.mark.parametrize("total", ["1180.01", "1179.99", "0", "-1180.00", 1181])
def test_an_expected_total_that_differs_by_any_amount_is_refused(total):
    assert codes_of(expected_total=total) == {"amount_mismatch"}


@pytest.mark.parametrize("amount", ["0", 0, 0.0, "0.00", "-0"])
def test_a_zero_tds_amount_in_any_form_is_no_tds(amount):
    assert preview(job(tds={"applicable": False, "section": None, "ledger": None, "amount": amount})).xml == \
        GOLDEN.read_bytes()


@pytest.mark.parametrize("amount", ["1e3", "NaN", "1e999999999", True, "0.001"])
def test_a_malformed_tds_amount_is_refused_without_crashing(amount):
    assert "invalid_amount" in codes_of(job(tds={"applicable": False, "section": None, "ledger": None,
                                                 "amount": amount}))


# ====================== signs and ISDEEMEDPOSITIVE, line by line ======================


def test_every_entry_has_the_sign_and_flags_of_its_side():
    mapping = MappingTable.from_rows([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": PURCHASE}])
    lines = [line("Purchase - RM", "1000.00"), line(RND, "250.50"), line(DISCOUNT, "70.50", False)]
    p = preview(job(lines=lines), mapping=mapping, expected_total="1180.00")
    party, *rest = entries(p.xml)
    # the party (supplier) is credited: positive AMOUNT, ISDEEMEDPOSITIVE No, ISPARTYLEDGER Yes
    assert (party.findtext("LEDGERNAME"), party.findtext("ISDEEMEDPOSITIVE"), party.findtext("ISPARTYLEDGER"),
            party.findtext("AMOUNT")) == (VENDOR, "No", "Yes", "1180.00")
    expected = [(PURCHASE, "Yes", "-1000.00"), (RND, "Yes", "-250.50"), (DISCOUNT, "No", "70.50")]
    assert [(e.findtext("LEDGERNAME"), e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT")) for e in rest] == expected
    assert all(e.findtext("ISPARTYLEDGER") == "No" for e in rest)
    for e in entries(p.xml):  # Tally's convention, as in the real export: deemed positive exactly when negative
        assert (e.findtext("ISDEEMEDPOSITIVE") == "Yes") == Decimal(e.findtext("AMOUNT")).is_signed()
    assert sum(Decimal(e.findtext("AMOUNT")) for e in entries(p.xml)) == 0
    assert p.lines == ((PURCHASE, Decimal("1000.00"), True), (RND, Decimal("250.50"), True),
                       (DISCOUNT, Decimal("70.50"), False))


def test_entries_keep_the_draft_line_order():
    forward = preview(job(lines=MULTI_LINES))
    backward = preview(job(lines=list(reversed(MULTI_LINES))))
    assert [e.findtext("LEDGERNAME") for e in entries(backward.xml)][1:] == [DISCOUNT, RND, PURCHASE]
    assert forward.party_amount == backward.party_amount and forward.xml != backward.xml


# ====================== escaping and control characters ======================


@pytest.mark.parametrize("char,entity", [("&", b"&amp;"), ("<", b"&lt;"), (">", b"&gt;"), ('"', b"&quot;"),
                                         ("'", b"&apos;")])
@pytest.mark.parametrize("field", ["reference", "narration", "party", "line_ledger", "company"])
def test_xml_special_characters_are_escaped_in_every_text_field(field, char, entity):
    text = f"A{char}B{char}"
    j, kw = _place(field, text)
    p = preview(j, **kw)
    root = ET.fromstring(p.xml)
    written = {"reference": ".//VOUCHER/REFERENCE", "narration": ".//VOUCHER/NARRATION",
               "party": ".//VOUCHER/PARTYLEDGERNAME", "line_ledger": ".//ALLLEDGERENTRIES.LIST[2]/LEDGERNAME",
               "company": ".//SVCURRENTCOMPANY"}[field]
    assert root.findtext(written).startswith(text)
    assert entity in p.xml and f"A{char}B".encode() not in p.xml
    assert_voucher_import(p.xml)  # still exactly one company-pinned VOUCHER ACTION="Create"


_REFUSED_CHARS = [chr(c) for c in range(0x20)] + ["\x7f", "\x80", "\x85", "\x9f", "\u2028", "\u2029", "\ud800",
                                                  "\udfff", "\ufffe", "\uffff"]


@pytest.mark.parametrize("char", _REFUSED_CHARS, ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("field", ["narration_start", "narration", "narration_end", "reference", "reference_end",
                                   "party", "line_ledger", "mapped_ledger", "company"])
def test_control_characters_are_refused_never_stripped(monkeypatch, field, char):
    # Before the fix a leading or trailing control character in the narration was stripped, C1 controls,
    # U+2028/U+2029 and U+FFFE/U+FFFF were written, and a lone surrogate crashed with UnicodeEncodeError.
    place = {"narration_start": ("narration", f"{char}Test"), "narration_end": ("narration", f"Test{char}"),
             "reference_end": ("reference", f"TEST-INV-001{char}")}.get(field, (field, f"Te{char}st"))
    if field == "mapped_ledger":  # whatever name resolution hands back is checked before it is written
        bad = place[1]
        monkeypatch.setattr(voucher_xml, "resolve_ledger",
                            lambda name, *a, **k: LedgerResolution(name, LedgerStatus.EXISTS, bad, "test"))
        place = ("line_ledger", "Purchase - RM")
    j, kw = _place(*place)
    with pytest.raises(DraftRejected) as info:
        preview(j, **kw)
    assert ("company_mismatch" if field == "company" else "invalid_text") in info.value.codes


@pytest.mark.parametrize("text", ["क्\u200dष", "न\u200cहीं", "€ ₹ £", "𝐀𝐁 😀", "Cafe\u0301", "A\u00a0B", "बिल।"])
def test_printable_unicode_including_joiners_and_astral_characters_round_trips(text):
    j, kw = _place("narration", text)
    p = preview(j, **kw)
    assert ET.fromstring(p.xml).findtext(".//VOUCHER/NARRATION") == f"{text} finledger:document_id={DOC}"


def _place(field: str, text: str):
    """(job, preview kwargs) with ``text`` in one field; masters are adjusted so only the text itself matters."""
    kw: dict = {}
    if field == "reference":
        return job(bill_wise={"ref_type": "new_ref", "ref": text, "due_date": None}), kw
    if field == "narration":
        return job(narration=text), kw
    if field == "party":
        kw["masters"] = LedgerMasters(COMPANY.guid, MASTERS.ledgers | {text})
        return job(party_ledger=text), kw
    if field == "line_ledger":
        kw["masters"] = LedgerMasters(COMPANY.guid, MASTERS.ledgers | {text})
        return job(lines=[line(text, "1180.00")]), kw
    if field == "company":
        kw.update(company=TallyCompany(text, COMPANY.guid), expected_company_name=text)
        return job(), kw
    raise AssertionError(field)


# ====================== Unicode ======================


def unicode_job() -> tuple[dict, LedgerMasters]:
    vendor, steel, freight = "श्री गणेश मेटल्स & Sons", "Purchase – ₹ <Steel>", 'Freight "Inward"'
    masters = LedgerMasters(COMPANY.guid, frozenset({vendor, steel, freight, DISCOUNT}))
    return job(party_ledger=vendor, narration="खरीद\u200cबिल ₹1180 <steel> & freight",
               bill_wise={"ref_type": "new_ref", "ref": "बिल/2026-27/'001'", "due_date": None},
               lines=[line(steel, "1000.00"), line(freight, "200.50"), line(DISCOUNT, "20.50", False)]), masters


def test_devanagari_rupee_and_escaping_match_the_reviewed_unicode_golden():
    j, masters = unicode_job()
    p = preview(j, masters=masters, expected_total="1180.00")
    golden = UNICODE_GOLDEN.read_bytes()
    assert p.xml == golden and p.sha256 == hashlib.sha256(golden).hexdigest() == UNICODE_SHA256
    assert not golden.startswith(b"\xef\xbb\xbf") and b"\r" not in golden
    root = ET.fromstring(p.xml)
    assert root.findtext(".//PARTYLEDGERNAME") == "श्री गणेश मेटल्स & Sons"
    assert root.findtext(".//REFERENCE") == "बिल/2026-27/'001'"
    assert "₹".encode() in p.xml and "\u200c".encode() in p.xml  # written as UTF-8 code points, joiner kept
    assert_voucher_import(p.xml)


@pytest.mark.parametrize("draft_name,master_name", [("Cafe\u0301", "Caf\u00e9"), ("Caf\u00e9", "Cafe\u0301")])
def test_unicode_names_are_never_normalised_to_match_a_ledger(draft_name, master_name):
    masters = LedgerMasters(COMPANY.guid, MASTERS.ledgers | {master_name})
    assert codes_of(job(lines=[line(draft_name, "1180.00")]), masters=masters) & {"missing_ledger", "ambiguous_ledger"}
    assert codes_of(job(party_ledger=draft_name), masters=masters) & {"missing_ledger", "ambiguous_ledger"}


# ====================== narration marker ======================


@pytest.mark.parametrize("narration,written", [("Test purchase", "Test purchase"),
                                               ("  Test purchase  ", "Test purchase"),
                                               ("", ""), ("   ", ""), ("बिल ₹1180", "बिल ₹1180")])
def test_the_marker_is_appended_once_after_one_space(narration, written):
    p = preview(job(narration=narration))
    marker = f"finledger:document_id={DOC}"
    assert p.narration == (f"{written} {marker}" if written else marker)
    assert ET.fromstring(p.xml).findtext(".//NARRATION") == p.narration
    assert _EXACT_MARKER.findall(p.narration) == [DOC] and p.xml.count(b"finledger:document_id=") == 1


def test_the_marker_depends_only_on_the_document_id():
    j2 = job(narration="Test purchase", lines=MULTI_LINES, date="2026-05-02")
    j2.update(revision=7, idempotency_key=f"{DOC}:r7")
    assert preview(j2, expected_total="1180.00").narration == preview().narration


# ====================== digest and byte determinism ======================


def _deep_reversed(value):
    if isinstance(value, dict):
        return {k: _deep_reversed(v) for k, v in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_deep_reversed(v) for v in value]  # list order is meaning (lines), so only mappings are reversed
    return value


@pytest.mark.parametrize("golden,lines", [(GOLDEN, None), (MULTILINE, MULTI_LINES)])
def test_bytes_and_digest_do_not_depend_on_key_order_at_any_depth(golden, lines):
    j = job(lines=lines) if lines else job()
    p = preview(_deep_reversed(j))
    assert p.xml == golden.read_bytes() and p.sha256 == hashlib.sha256(golden.read_bytes()).hexdigest()


def test_bytes_do_not_depend_on_mapping_row_or_masters_order():
    rows = [{"kind": "ledger", "our_key": f"Key {i}", "erp_value": PURCHASE if i == 0 else f"L{i}"} for i in range(30)]
    names = [VENDOR, PURCHASE, RND, DISCOUNT] + [f"L{i}" for i in range(1, 30)]
    j = job(lines=[line("Key 0", "1180.00")])
    a = preview(j, mapping=MappingTable.from_rows(rows), masters=LedgerMasters(COMPANY.guid, frozenset(names)))
    b = preview(j, mapping=MappingTable.from_rows(reversed(rows)),
                masters=LedgerMasters(COMPANY.guid, frozenset(reversed(names))))
    assert a.xml == b.xml == GOLDEN.read_bytes() and a.sha256 == GOLDEN_SHA256


_SEED_SCRIPT = """
import sys
from test_voucher_xml import COMPANY, PURCHASE, VENDOR, job, preview
from finledger_tally import LedgerMasters
from finledger_tally.mapping import MappingTable
names = {VENDOR, PURCHASE, "R&D <Supplies>", "Discount Received"} | {f"Ledger {i}" for i in range(200)}
rows = [{"kind": "ledger", "our_key": f"Key {i}", "erp_value": f"Ledger {i}"} for i in range(50)]
lines = [{"ledger": "Key 7", "amount": "1000.00", "is_debit": True},
         {"ledger": "R&D <Supplies>", "amount": "200.00", "is_debit": True},
         {"ledger": "Discount Received", "amount": "20.00", "is_debit": False}]
masters = LedgerMasters(COMPANY.guid, frozenset(names))
p = preview(job(lines=lines), masters=masters, mapping=MappingTable.from_rows(rows))
sys.stdout.write(p.sha256)
"""


def test_bytes_are_identical_across_processes_with_different_hash_seeds():
    path = os.pathsep.join([str(HERE), str(ROOT / "person4_tally" / "src"), str(ROOT / "packages")])
    digests = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run([sys.executable, "-B", "-c", _SEED_SCRIPT], env=env, cwd=HERE, capture_output=True,
                             text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        digests.add(out.stdout)
    assert len(digests) == 1 and re.fullmatch(r"[0-9a-f]{64}", digests.pop())


def test_existing_goldens_are_unchanged():
    assert hashlib.sha256(GOLDEN.read_bytes()).hexdigest() == GOLDEN_SHA256
    assert preview(job(lines=MULTI_LINES)).xml == MULTILINE.read_bytes()


# ====================== scope: what is refused, and how ======================


@pytest.mark.parametrize("vtype", ["debit_note", "credit_note"])
def test_notes_are_refused_precisely_and_never_reach_the_purchase_template(monkeypatch, vtype):
    def no_template(*a, **k):
        raise AssertionError("the purchase template was used")
    monkeypatch.setattr(voucher_xml, "_render", no_template)
    for bill_wise in ({"ref_type": "new_ref", "ref": "CN-1"}, {"ref_type": "against_ref", "ref": "TEST-INV-001"}):
        with pytest.raises(DraftRejected) as info:
            preview(job(voucher_type=vtype, bill_wise=bill_wise))
        why = dict(info.value.reasons)["unsupported_voucher_type"]
        assert "original invoice" in why and "#8" in why


def test_payments_are_refused_as_out_of_scope():
    with pytest.raises(DraftRejected) as info:
        preview(job(voucher_type="payment"))
    assert "out of scope" in dict(info.value.reasons)["unsupported_voucher_type"]


@pytest.mark.parametrize("gst", [
    {"treatment": "intra", "input_ledgers": [], "tax_breakup": {}},
    {"treatment": "inter", "input_ledgers": [], "tax_breakup": {}},
    {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {"cgst": 0}},
    {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {"cgst": 0.0, "sgst": 0.0}},
    {"treatment": "exempt", "input_ledgers": [""], "tax_breakup": {}},
])
def test_any_gst_data_at_all_is_refused_even_zero(gst):
    assert "unsupported_tax" in codes_of(job(gst=gst))


@pytest.mark.parametrize("tds", [{"applicable": True}, {"section": "194C"}, {"ledger": "TDS Payable"},
                                 {"amount": "0.01"}, {"amount": "-0.01"}])
def test_any_tds_data_at_all_is_refused(tds):
    assert "unsupported_tax" in codes_of(job(tds={"applicable": False, "section": None, "ledger": None, "amount": "0",
                                                  **tds}))


def test_nothing_but_the_fixed_template_is_ever_written():
    for j, kw in ((job(), {}), (job(lines=MULTI_LINES), {"expected_total": "1180.00"}), unicode_job_kw()):
        p = preview(j, **kw)
        root = ET.fromstring(p.xml)
        assert {e.tag for e in root.iter()} == TEMPLATE_TAGS  # no tax, rate, round-off, due date or total element
        amounts = sorted(Decimal(a.text) for a in root.iter("AMOUNT"))
        line_amounts = [-a if d else a for _, a, d in p.lines]
        assert amounts == sorted([p.party_amount, p.party_amount] + line_amounts)  # party + its bill, then the lines


def unicode_job_kw():
    j, masters = unicode_job()
    return j, {"masters": masters}


@pytest.mark.parametrize("j,kw", [(job(), {}), (job(), {"masters": None}), (job(voucher_type="credit_note"), {}),
                                  (job(party_ledger="Ghost"), {}), (job(date="2026-W14-3"), {})])
def test_the_preview_cannot_open_a_connection(monkeypatch, j, kw):
    def no_network(*a, **k):
        raise AssertionError("the preview tried to reach the network")
    for target, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                         (TallyTransport, "export"), (TallyTransport, "import_vouchers"),
                         (TallyTransport, "_connect")):
        monkeypatch.setattr(target, name, no_network)
    try:
        p = preview(j, **kw)
    except DraftRejected as exc:
        assert exc.codes
    else:
        assert p.xml == GOLDEN.read_bytes()


def test_a_preview_is_never_described_as_posted():
    p = preview()
    assert p.stage == "preview_ready"
    fields = {f.name for f in dataclasses.fields(VoucherPreview)}
    assert not fields & {"erp_id", "posted", "sent", "state", "master_id", "alter_id", "voucher_guid"}
    assert b"posted" not in p.xml.lower() and "posted" not in p.narration.lower()
    with pytest.raises(DraftRejected) as info:
        preview(job(party_ledger="Ghost"))
    assert not any("posted" in code for code in info.value.codes)
