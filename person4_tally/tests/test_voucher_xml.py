"""Purchase-voucher XML preview. Inputs mirror the real voucher captured from TallyPrime EDU 7.1; the Import XML
itself has never been sent to Tally (preview only)."""

from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

from finledger_tally import (
    DraftRejected,
    LedgerMasters,
    TallyCompany,
    identity_marker,
    parse_company_list,
    preview_purchase_xml,
    resolve_company,
)
from finledger_tally.identity import _EXACT_MARKER

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
GOLDEN = HERE / "fixtures" / "preview" / "purchase_exempt_1180.xml"
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
OTHER_DOC = "11111111-1111-4111-8111-111111111111"
COMPANY = resolve_company(parse_company_list((REAL / "response_01_companies.xml").read_bytes()), "FinLedger Test Company")
VENDOR, PURCHASE = "FinLedger Test Vendor", "FinLedger Test Purchase"
MASTERS = LedgerMasters(COMPANY.guid, frozenset({VENDOR, PURCHASE, "Discount Received", "R&D <Supplies>"}))


def job(**draft_changes) -> dict:
    draft = {
        "document_id": DOC, "date": "2026-04-01", "voucher_type": "purchase",
        "party_ledger": VENDOR, "party_create_proposal": False,
        "lines": [{"ledger": PURCHASE, "amount": "1180.00", "is_debit": True, "cost_centre": None}],
        "gst": {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {}},
        "tds": {"applicable": False, "section": None, "ledger": None, "amount": "0"},
        "bill_wise": {"ref_type": "new_ref", "ref": "TEST-INV-001", "due_date": None},
        "narration": "Test purchase", "attachment_document_id": DOC, "reasons": ["test"],
        "map_trace_id": "22222222-2222-4222-8222-222222222222",
    }
    draft.update(draft_changes)
    return {"document_id": DOC, "client_id": "33333333-3333-4333-8333-333333333333", "revision": 1,
            "idempotency_key": f"{DOC}:r1", "voucher_draft": draft}


def preview(j=None, **kw):
    args = {"company": COMPANY, "expected_company_name": "FinLedger Test Company", "masters": MASTERS}
    args.update(kw)
    return preview_purchase_xml(j if j is not None else job(), **args)


def rejected(j=None, **kw) -> set[str]:
    with pytest.raises(DraftRejected) as info:
        preview(j, **kw)
    return info.value.codes


def entries(xml: bytes) -> list[ET.Element]:
    return ET.fromstring(xml).findall(".//VOUCHER/ALLLEDGERENTRIES.LIST")


# ---------- valid drafts ----------


def test_valid_draft_renders_the_expected_voucher():
    p = preview(expected_total=Decimal("1180.00"))
    root = ET.fromstring(p.xml)
    assert root.findtext("HEADER/TALLYREQUEST") == "Import Data"
    assert root.findtext(".//SVCURRENTCOMPANY") == "FinLedger Test Company"
    [voucher] = root.findall(".//TALLYMESSAGE/VOUCHER")
    assert voucher.get("VCHTYPE") == "Purchase" and voucher.get("ACTION") == "Create"
    assert voucher.findtext("DATE") == "20260401"
    assert voucher.findtext("REFERENCE") == "TEST-INV-001"
    assert voucher.findtext("PARTYLEDGERNAME") == VENDOR
    assert voucher.findtext("NARRATION") == f"Test purchase finledger:document_id={DOC}"
    party, purchase = entries(p.xml)
    assert (party.findtext("LEDGERNAME"), party.findtext("ISDEEMEDPOSITIVE"), party.findtext("AMOUNT")) == \
        (VENDOR, "No", "1180.00")
    assert (party.findtext("BILLALLOCATIONS.LIST/NAME"), party.findtext("BILLALLOCATIONS.LIST/BILLTYPE"),
            party.findtext("BILLALLOCATIONS.LIST/AMOUNT")) == ("TEST-INV-001", "New Ref", "1180.00")
    assert (purchase.findtext("LEDGERNAME"), purchase.findtext("ISDEEMEDPOSITIVE"), purchase.findtext("AMOUNT")) == \
        (PURCHASE, "Yes", "-1180.00")
    assert p.party_amount == Decimal("1180.00") and p.company_guid == COMPANY.guid
    assert p.idempotency_key == f"{DOC}:r1" and p.not_written == ()


def test_signs_match_the_real_tally_export():
    # Real export: vendor +1180.00 / ISDEEMEDPOSITIVE No; purchase -1180.00 / Yes.
    real = ET.fromstring((REAL / "response_02_day_before_edit.xml").read_bytes()).find(".//DATA/COLLECTION/VOUCHER")
    real_lines = {e.findtext("LEDGERNAME"): (e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT"))
                  for e in real.findall("LEDGERENTRIES.LIST")}
    ours = {e.findtext("LEDGERNAME"): (e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT")) for e in entries(preview().xml)}
    assert ours == real_lines
    assert real.findtext("REFERENCE") == ET.fromstring(preview().xml).findtext(".//REFERENCE")
    assert real.findtext("DATE") == ET.fromstring(preview().xml).findtext(".//DATE")


def test_matches_the_reviewed_golden_file():
    assert preview().xml == GOLDEN.read_bytes()


def test_never_sends_identity_or_numbering_fields():
    xml = preview().xml
    for forbidden in (b"GUID", b"REMOTEID", b"MASTERID", b"ALTERID", b"VOUCHERNUMBER", b"VCHKEY"):
        assert forbidden not in xml
    assert xml.count(b"ACTION=") == 1 and xml.count(b"<VOUCHER ") == 1
    assert b"<LEDGER " not in xml and b"<LEDGER>" not in xml  # no ledger master is ever created


def test_entries_balance_to_zero():
    j = job(lines=[{"ledger": PURCHASE, "amount": "1000.00", "is_debit": True},
                   {"ledger": "R&D <Supplies>", "amount": "250.50", "is_debit": True},
                   {"ledger": "Discount Received", "amount": "70.50", "is_debit": False}])
    p = preview(j, expected_total=Decimal("1180.00"))
    amounts = [Decimal(e.findtext("AMOUNT")) for e in entries(p.xml)]
    assert sum(amounts) == 0 and amounts[0] == Decimal("1180.00")
    names = [e.findtext("LEDGERNAME") for e in entries(p.xml)]
    assert names == [VENDOR, PURCHASE, "R&D <Supplies>", "Discount Received"]
    assert b"R&amp;D &lt;Supplies&gt;" in p.xml


def test_empty_narration_still_carries_the_marker():
    assert preview(job(narration="")).narration == identity_marker(DOC)


def test_due_date_is_reported_as_not_written():
    j = job(bill_wise={"ref_type": "new_ref", "ref": "TEST-INV-001", "due_date": "2026-05-01"})
    p = preview(j)
    assert p.not_written and b"20260501" not in p.xml and b"DUE" not in p.xml.upper()


def test_p3_ai_paragraph_is_accepted_but_never_written():
    j = job(ai_paragraph="Looks fine <script>")
    assert preview(j).xml == preview().xml


# ---------- determinism and marker stability ----------


@pytest.mark.parametrize("amount", ["1180.00", "1180", 1180, 1180.0, "1180.0", " 1180.00 "])
def test_same_money_in_any_json_form_gives_identical_bytes(amount):
    j = job(lines=[{"ledger": PURCHASE, "amount": amount, "is_debit": True, "cost_centre": None}])
    assert preview(j).xml == GOLDEN.read_bytes()


def test_key_order_does_not_change_bytes():
    j = job()
    j["voucher_draft"] = dict(reversed(list(j["voucher_draft"].items())))
    assert preview(j).xml == GOLDEN.read_bytes()


def test_marker_is_the_same_for_every_revision():
    j2 = job()
    j2.update(revision=2, idempotency_key=f"{DOC}:r2")
    p1, p2 = preview(), preview(j2)
    assert p1.xml == p2.xml and p1.narration == p2.narration
    assert p2.idempotency_key.endswith(":r2")


def test_marker_is_exact_and_found_by_the_lookup_pattern():
    narration = preview().narration
    assert _EXACT_MARKER.findall(narration) == [DOC]
    other = job(document_id=OTHER_DOC, attachment_document_id=OTHER_DOC)
    other.update(document_id=OTHER_DOC, idempotency_key=f"{OTHER_DOC}:r1")
    assert _EXACT_MARKER.findall(preview(other).narration) == [OTHER_DOC]


def test_input_is_not_mutated():
    j = job()
    before = copy.deepcopy(j)
    preview(j)
    assert j == before


# ---------- malformed drafts and jobs ----------


@pytest.mark.parametrize("change,code", [
    ({"date": "2026-4-1"}, "invalid_date"),
    ({"date": "2026-02-30"}, "invalid_date"),
    ({"date": "01-04-2026"}, "invalid_date"),
    ({"attachment_document_id": OTHER_DOC}, "invalid_contract"),
    ({"voucher_type": "journal"}, "invalid_contract"),
    ({"bill_wise": {"ref_type": "new_ref"}}, "invalid_contract"),
    ({"gst": {"treatment": "reverse_charge"}}, "invalid_contract"),
    ({"lines": []}, "invalid_contract"),
    ({"lines": [{"ledger": PURCHASE, "amount": "1180.001", "is_debit": True}]}, "invalid_amount"),
    ({"lines": [{"ledger": PURCHASE, "amount": "-1180.00", "is_debit": True}]}, "invalid_amount"),
    ({"lines": [{"ledger": PURCHASE, "amount": "0", "is_debit": True}]}, "invalid_amount"),
    ({"lines": [{"ledger": PURCHASE, "amount": "NaN", "is_debit": True}]}, "invalid_amount"),
    ({"lines": [{"ledger": PURCHASE, "amount": True, "is_debit": True}]}, "invalid_amount"),
    ({"lines": [{"ledger": PURCHASE, "amount": "1180.00", "is_debit": "yes"}]}, "invalid_contract"),
    ({"lines": [{"ledger": PURCHASE, "amount": "1180.00", "is_debit": True, "cost_centre": "HQ"}]}, "unsupported_field"),
    ({"lines": [{"ledger": VENDOR, "amount": "1180.00", "is_debit": True}]}, "invalid_contract"),
    ({"lines": [{"ledger": "Discount Received", "amount": "10.00", "is_debit": False}]}, "invalid_amount"),
    ({"narration": "two\\nlines".replace("\\n", "\n")}, "invalid_text"),
    ({"party_ledger": " FinLedger Test Vendor"}, "invalid_text"),
    ({"bill_wise": {"ref_type": "new_ref", "ref": "  "}}, "missing_reference"),
    ({"bill_wise": {"ref_type": "against_ref", "ref": "TEST-INV-001"}}, "unsupported_bill_wise"),
    ({"voucher_type": "debit_note"}, "unsupported_voucher_type"),
    ({"voucher_type": "credit_note"}, "unsupported_voucher_type"),
    ({"surprise": 1}, "unsupported_field"),
])
def test_malformed_drafts_are_rejected(change, code):
    assert code in rejected(job(**change))


@pytest.mark.parametrize("mutate,code", [
    (lambda j: j.update(revision=0), "invalid_job"),
    (lambda j: j.update(revision=True), "invalid_job"),
    (lambda j: j.update(idempotency_key=f"{DOC}:r2"), "invalid_job"),
    (lambda j: j.update(document_id=OTHER_DOC, idempotency_key=f"{OTHER_DOC}:r1"), "invalid_job"),
    (lambda j: j.update(extra="x"), "invalid_job"),
    (lambda j: j.pop("idempotency_key"), "invalid_job"),
    (lambda j: j.update(voucher_draft="not a draft"), "invalid_contract"),
    (lambda j: j["voucher_draft"].update(document_id="9D4F3D5D-499E-41DB-B510-E98348E5B503",
                                         attachment_document_id="9D4F3D5D-499E-41DB-B510-E98348E5B503"),
     "invalid_job"),
])
def test_malformed_jobs_are_rejected(mutate, code):
    j = job()
    mutate(j)
    assert code in rejected(j)


def test_every_problem_is_reported_at_once():
    codes = rejected(job(voucher_type="debit_note", party_ledger="Unknown Vendor",
                         gst={"treatment": "intra", "input_ledgers": [], "tax_breakup": {}}))
    assert {"unsupported_voucher_type", "missing_ledger", "unsupported_tax"} <= codes


# ---------- missing ledgers ----------


def test_missing_party_ledger_is_rejected_never_created():
    assert "missing_ledger" in rejected(job(party_ledger="New Vendor Pvt Ltd"))


def test_missing_line_ledger_is_rejected():
    assert "missing_ledger" in rejected(job(lines=[{"ledger": "Purchase - Steel", "amount": "1180.00", "is_debit": True}]))


def test_ledger_names_are_matched_exactly():
    # A case-only match is never chosen: it is reported as ambiguous, not silently used.
    codes = rejected(job(party_ledger=VENDOR.lower()))
    assert "ambiguous_ledger" in codes and "missing_ledger" not in codes
    codes = rejected(job(lines=[{"ledger": PURCHASE.upper(), "amount": "1180.00", "is_debit": True}]))
    assert "ambiguous_ledger" in codes


def test_create_proposal_is_rejected_even_if_the_ledger_now_exists():
    assert "party_create_proposal" in rejected(job(party_create_proposal=True))


def test_empty_masters_reject_everything():
    assert "missing_ledger" in rejected(masters=LedgerMasters(COMPANY.guid, frozenset()))


# ---------- amount mismatch ----------


def test_invoice_total_mismatch_is_rejected():
    assert "amount_mismatch" in rejected(expected_total=Decimal("1180.01"))
    assert "amount_mismatch" in rejected(expected_total=Decimal("2000.00"))


def test_credits_exceeding_debits_are_rejected():
    j = job(lines=[{"ledger": PURCHASE, "amount": "100.00", "is_debit": True},
                   {"ledger": "Discount Received", "amount": "100.00", "is_debit": False}])
    assert "amount_mismatch" in rejected(j)


def test_bad_expected_total_is_rejected():
    assert "invalid_amount" in rejected(expected_total=Decimal("1180.005"))


# ---------- unsupported tax data ----------


@pytest.mark.parametrize("gst", [
    {"treatment": "intra", "input_ledgers": [], "tax_breakup": {}},
    {"treatment": "inter", "input_ledgers": ["Input IGST"], "tax_breakup": {"igst": 180.0}},
    {"treatment": "exempt", "input_ledgers": ["Input CGST"], "tax_breakup": {}},
    {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {"cgst": 90.0}},
])
def test_gst_drafts_stay_blocked(gst):
    assert "unsupported_tax" in rejected(job(gst=gst))


@pytest.mark.parametrize("tds", [
    {"applicable": True, "section": "194C", "ledger": "TDS Payable", "amount": "11.80"},
    {"applicable": False, "section": None, "ledger": None, "amount": "11.80"},
    {"applicable": False, "section": "194J", "ledger": None, "amount": "0"},
])
def test_tds_stays_blocked(tds):
    assert "unsupported_tax" in rejected(job(tds=tds))


# ---------- company identity ----------


def test_wrong_paired_company_is_rejected():
    assert "company_mismatch" in rejected(expected_company_name="FinLedger Isolation Test")


def test_masters_from_another_company_are_rejected():
    other = LedgerMasters("6a250654-44be-4954-a9c8-6cd5e29f4e76", MASTERS.ledgers)
    assert "company_mismatch" in rejected(masters=other)


def test_unverified_company_guid_is_rejected():
    fake = TallyCompany(name="FinLedger Test Company", guid="not-a-guid")
    assert "company_mismatch" in rejected(company=fake, masters=LedgerMasters("not-a-guid", MASTERS.ledgers))


def test_company_name_is_escaped_into_the_pin():
    odd = TallyCompany(name="A & B <Co>", guid=COMPANY.guid)
    p = preview(company=odd, expected_company_name="A & B <Co>")
    assert ET.fromstring(p.xml).findtext(".//SVCURRENTCOMPANY") == "A & B <Co>"


# ---------- marker conflicts ----------


@pytest.mark.parametrize("narration", [
    f"Test purchase finledger:document_id={DOC}",
    f"finledger:document_id={OTHER_DOC}",
    "FinLedger Document_ID copied by hand",
])
def test_narration_that_already_has_marker_text_is_rejected(narration):
    assert "marker_conflict" in rejected(job(narration=narration))


# ---------- the module cannot send ----------


def test_preview_module_has_no_network_code():
    src = (HERE.parent / "src" / "finledger_tally" / "voucher_xml.py").read_text()
    assert not re.search(r"\bimport (socket|http|urllib|requests|httpx)\b|from (http|urllib)", src)


# ---------- hardening: mapping, masters, escaping, Unicode, decimals, determinism ----------

from finledger_tally.mapping import MappingTable  # noqa: E402


def test_ledger_mapping_translates_line_ledgers_exactly():
    mapping = MappingTable.from_rows([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": PURCHASE}])
    p = preview(job(lines=[{"ledger": "Purchase - RM", "amount": "1180.00", "is_debit": True}]), mapping=mapping)
    assert p.xml == GOLDEN.read_bytes()
    assert p.lines == ((PURCHASE, Decimal("1180.00"), True),)


def test_mapping_to_a_missing_ledger_is_rejected():
    mapping = MappingTable.from_rows([{"kind": "ledger", "our_key": PURCHASE, "erp_value": "Ghost Ledger"}])
    assert "missing_ledger" in rejected(mapping=mapping)


def test_mapping_onto_the_party_ledger_is_rejected():
    mapping = MappingTable.from_rows([{"kind": "ledger", "our_key": PURCHASE, "erp_value": VENDOR}])
    assert "invalid_contract" in rejected(mapping=mapping)


def test_no_masters_is_rejected():
    assert "masters_unavailable" in rejected(masters=None)


def test_xml_injection_in_every_text_field_is_escaped():
    evil = '</NARRATION><VOUCHER ACTION="Delete"><X a="1">&amp;\'"'
    names = {VENDOR: evil + "V", PURCHASE: evil + "P"}
    masters = LedgerMasters(COMPANY.guid, frozenset(names.values()))
    j = job(party_ledger=names[VENDOR], narration=evil,
            lines=[{"ledger": names[PURCHASE], "amount": "1180.00", "is_debit": True}],
            bill_wise={"ref_type": "new_ref", "ref": evil, "due_date": None})
    root = ET.fromstring(preview(j, masters=masters).xml)
    assert len(root.findall(".//VOUCHER")) == 1
    assert [e.get("ACTION") for e in root.iter() if "ACTION" in e.attrib] == ["Create"]
    assert root.findtext(".//PARTYLEDGERNAME") == names[VENDOR]
    assert root.findtext(".//REFERENCE") == evil
    assert root.findtext(".//NARRATION") == f"{evil} finledger:document_id={DOC}"


def test_unicode_names_round_trip_as_utf8():
    vendor, ledger = "श्री गणेश मेटल्स", "Purchase – Café ₹"
    masters = LedgerMasters(COMPANY.guid, frozenset({vendor, ledger}))
    j = job(party_ledger=vendor, narration="बिल ₹1180",
            lines=[{"ledger": ledger, "amount": "1180.00", "is_debit": True}])
    p = preview(j, masters=masters)
    assert p.xml.decode("utf-8")  # valid UTF-8
    root = ET.fromstring(p.xml)
    assert root.findtext(".//PARTYLEDGERNAME") == vendor
    assert [e.findtext("LEDGERNAME") for e in entries(p.xml)] == [vendor, ledger]
    assert p.xml == preview(j, masters=masters).xml


@pytest.mark.parametrize("amount,ok", [
    ("0.01", True), ("0.1", True), ("1", True), ("999999999999.99", True), ("1180.10", True), ("1000000000000.00", False),
    ("0.001", False), ("0.00", False), ("-0.01", False), ("Infinity", False), ("-Infinity", False),
    ("1e400", False), ("1180.005", False), ("", False), ("12,000.00", False),
])
def test_decimal_boundaries(amount, ok):
    j = job(lines=[{"ledger": PURCHASE, "amount": amount, "is_debit": True}])
    if ok:
        p = preview(j)
        assert p.party_amount == Decimal(amount).quantize(Decimal("0.01"))
        assert [e.findtext("AMOUNT") for e in entries(p.xml)] == [f"{p.party_amount:.2f}", f"-{p.party_amount:.2f}"]
    else:
        codes = rejected(j)
        assert codes & {"invalid_amount", "invalid_contract"}


def test_amounts_are_never_rounded():
    # 1180.10 stays 1180.10 (never 1180.1 or 1180), and float noise is refused rather than rounded.
    assert "invalid_amount" in rejected(job(lines=[{"ledger": PURCHASE, "amount": 0.1 + 0.2, "is_debit": True}]))
    assert preview(job(lines=[{"ledger": PURCHASE, "amount": "1180.10", "is_debit": True}])).party_amount == \
        Decimal("1180.10")


@pytest.mark.parametrize("d", ["2026-13-01", "2026-00-10", "2025-02-29", "20260401", "2026/04/01", "", " 2026-04-01",
                               "2026-04-01T00:00:00"])
def test_invalid_dates(d):
    assert rejected(job(date=d)) & {"invalid_date", "invalid_contract"}


def test_leap_day_is_valid():
    assert ET.fromstring(preview(job(date="2028-02-29")).xml).findtext(".//DATE") == "20280229"


def test_repeated_previews_have_the_same_marker_and_hash():
    first, second = preview(), preview(copy.deepcopy(job()))
    assert first.sha256 == second.sha256 == "d9e312e455c5b290ae281d816640685ebf78cbb013989032695ff4a14de58dd3"
    assert first.narration == second.narration


def test_preview_metadata_is_stable():
    p = preview()
    assert (p.voucher_type, p.voucher_date, p.reference, p.stage) == ("purchase", "20260401", "TEST-INV-001",
                                                                     "preview_ready")


def test_multi_line_purchase_with_a_credit_line_matches_its_golden_file():
    lines = [{"ledger": PURCHASE, "amount": "1000.00", "is_debit": True},
             {"ledger": "R&D <Supplies>", "amount": "200.00", "is_debit": True},
             {"ledger": "Discount Received", "amount": "20.00", "is_debit": False}]
    p = preview(job(lines=lines), expected_total=Decimal("1180.00"))
    golden = HERE / "fixtures" / "preview" / "purchase_exempt_multiline_1180.xml"
    assert p.xml == golden.read_bytes()
    amounts = [Decimal(e.findtext("AMOUNT")) for e in entries(p.xml)]
    assert amounts == [Decimal("1180.00"), Decimal("-1000.00"), Decimal("-200.00"), Decimal("20.00")]
    assert sum(amounts) == 0


@pytest.mark.parametrize("vtype", ["debit_note", "credit_note", "payment"])
def test_other_voucher_types_stay_blocked(vtype):
    assert "unsupported_voucher_type" in rejected(job(voucher_type=vtype))
