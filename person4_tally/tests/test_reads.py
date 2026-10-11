"""Lookup (reconciliation reads), open bills, health, mapping and journal. Company list and voucher rows come from the
real 2026-10-10 capture; ledger, bill and Import replies are SYNTHETIC (fake_tally.py)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fake_tally import FakeTally, bill
from finledger_tally import (
    AgentSettings,
    CompanyNotResolved,
    FileJournal,
    Health,
    JournalCorrupt,
    LedgerMasters,
    LedgerStatus,
    LookupStatus,
    MappingTable,
    Phase,
    TallyCompany,
    TallyConnector,
    TallyMalformedResponse,
    TallyTransport,
    lookup_document,
    masters_snapshot_payloads,
    parse_bill_export,
    parse_voucher_export,
    find_by_marker,
    resolve_ledger,
    resolve_party,
)
from finledger_tally.journal import gate
from test_isolation_check import OTHER_CO, TEST_CO, voucher, vouchers_reply
from test_masters import ledgers_reply

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
COMPANY = TallyCompany(*TEST_CO)
DAY = date(2026, 4, 1)


@pytest.fixture
def tally():
    fakes = []

    def start(**kw):
        fake = FakeTally(**kw)
        fakes.append(fake)
        return fake

    yield start
    for fake in fakes:
        fake.close()


def T(fake, **kw):
    return TallyTransport(port=fake.port, timeout=kw.get("timeout", 2), max_reply_bytes=kw.get("max", 64 * 1024 * 1024))


# ---------- reconciliation reads on the REAL voucher replies ----------


def test_real_voucher_is_found_before_and_after_the_amount_edit():
    before = parse_voucher_export((REAL / "response_03_fy_before_edit.xml").read_bytes())
    after = parse_voucher_export((REAL / "response_06_fy_after_edit.xml").read_bytes())
    a, b = find_by_marker(before, DOC, TEST_CO[1]), find_by_marker(after, DOC, TEST_CO[1])
    assert a.status is b.status is LookupStatus.FOUND
    assert (a.row.guid, a.row.master_id) == (b.row.guid, b.row.master_id)  # identity unchanged by the edit
    assert (a.row.alter_id, b.row.alter_id) == (3, 4)  # ALTERID records that it changed
    assert (a.row.amount, b.row.amount) == (Decimal("1180.00"), Decimal("2000.00"))


def test_lookup_found(tally):
    fake = tally()
    fake.add_voucher(TEST_CO[1], narration=f"x finledger:document_id={DOC}")
    result, company = lookup_document(T(fake), TEST_CO[0], DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.FOUND and company == COMPANY
    assert fake.kinds == ["Company", "Voucher", "Company"]


def test_lookup_not_found_says_it_is_not_proof(tally):
    result, _ = lookup_document(T(tally()), TEST_CO[0], DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.NOT_FOUND and "does not prove" in result.reason


def test_lookup_multiple_candidates_are_ambiguous(tally):
    fake = tally()
    for _ in range(2):
        fake.add_voucher(TEST_CO[1], narration=f"x finledger:document_id={DOC}")
    assert lookup_document(T(fake), TEST_CO[0], DOC, DAY, TEST_CO[1])[0].status is LookupStatus.AMBIGUOUS


def test_lookup_foreign_company_voucher_is_unreadable(tally):
    fake = tally()
    fake.vouchers[TEST_CO[1]].append(voucher(OTHER_CO[1], 9))
    result, _ = lookup_document(T(fake), TEST_CO[0], DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and "not company-scoped" in result.reason


@pytest.mark.parametrize("blocks,why", [
    (lambda: [voucher(TEST_CO[1], 1, doc=None), voucher(TEST_CO[1], 1, doc=None)], "share guid"),
    (lambda: [voucher(TEST_CO[1], 1, doc=None),
              voucher(TEST_CO[1], 1, doc=None, guid=f"{TEST_CO[1]}-00000002")], "share master_id"),
    (lambda: [voucher(TEST_CO[1], 1, doc=None, guid="12345")], "not <company GUID>"),
    (lambda: [voucher(TEST_CO[1], 1, doc="0" * 8 + DOC[8:]).replace(
        "finledger:document_id=", "finledger: document_id=")], "marker-like"),
])
def test_lookup_malformed_or_duplicate_candidates_are_unreadable(tally, blocks, why):
    fake = tally()
    fake.vouchers[TEST_CO[1]] = blocks()
    result, _ = lookup_document(T(fake), TEST_CO[0], DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and why in result.reason


def test_lookup_oversized_reply_is_unreadable_not_absent(tally):
    fake = tally()
    for n in range(1, 40):
        fake.add_voucher(TEST_CO[1], narration="filler " * 20, master_id=n)
    result, _ = lookup_document(T(fake, max=4096), TEST_CO[0], DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and "ReplyTooLarge" in result.reason


def test_lookup_transport_failure_is_unreadable_not_absent(tally):
    fake = tally()
    fake.down = True
    assert lookup_document(T(fake), TEST_CO[0], DOC, DAY, TEST_CO[1])[0].status is LookupStatus.UNREADABLE


def test_lookup_wrong_company_guid_is_unreadable(tally):
    result, company = lookup_document(T(tally()), TEST_CO[0], DOC, DAY, OTHER_CO[1])
    assert result.status is LookupStatus.UNREADABLE and company is None


# ---------- open bills (synthetic shape) ----------


def test_open_bills_valid_and_platform_payload(tally):
    fake = tally()
    fake.bills[TEST_CO[1]] = [bill("TEST-INV-001", "FinLedger Test Vendor", "1180.00", "1180.00"),
                              bill("ADV-1", "FinLedger Test Vendor", "-500.00", "-500.00", billdate="")]
    bills = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port)) \
        .fetch_open_bills()
    assert [b.bill_ref for b in bills.bills] == ["ADV-1", "TEST-INV-001"]
    payload = bills.platform_payload()
    assert payload["bills"] == [{"party_ledger": "FinLedger Test Vendor", "vendor_gstin": None,
                                 "bill_ref": "TEST-INV-001", "bill_date": "2026-04-01", "due_date": None,
                                 "amount": "1180.00", "pending_amount": "1180.00"}]


def test_zero_bills_is_an_empty_result_not_a_failure(tally):
    fake = tally()
    bills = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port)) \
        .fetch_open_bills()
    assert bills.bills == () and bills.platform_payload()["bills"] == []


MASTERS = LedgerMasters(TEST_CO[1], frozenset({"FinLedger Test Vendor"}), (("FinLedger Test Vendor", "Sundry Creditors"),),
                        TEST_CO[0])


@pytest.mark.parametrize("rows,error", [
    ([bill("X", "Other Co Vendor", "1.00", "1.00")], CompanyNotResolved),
    ([bill("X", "FinLedger Test Vendor", "1.00", "1.00")] * 2, TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1,180.00", "1.00")], TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1180.00 Cr", "1.00")], TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1.00", "")], TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1.00", "1.00", billdate="20260231")], TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1.00", "1.00", billdate="1-Apr-26")], TallyMalformedResponse),
    ([bill(" X", "FinLedger Test Vendor", "1.00", "1.00")], TallyMalformedResponse),
    ([bill("X", "FinLedger Test Vendor", "1.00", "1.00").replace("BILL", "LEDGER")], TallyMalformedResponse),
])
def test_bad_bill_replies_are_refused_whole(rows, error):
    with pytest.raises(error):
        parse_bill_export(ledgers_reply(rows), COMPANY, MASTERS)


def test_bills_with_masters_of_another_company_are_refused():
    with pytest.raises(CompanyNotResolved):
        parse_bill_export(ledgers_reply([]), TallyCompany(*OTHER_CO), MASTERS)


def test_failed_bill_fetch_raises(tally):
    fake = tally()
    fake.down = True
    from finledger_tally import TallyUnavailable
    with pytest.raises(TallyUnavailable):
        TallyConnector(AgentSettings(company_name=TEST_CO[0], port=fake.port)).fetch_open_bills()


# ---------- health ----------


def H(fake, **kw):
    fields = dict(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port, timeout=2)
    fields.update(kw)
    return TallyConnector(AgentSettings(**fields)).health()


def test_health_verified_with_masters(tally):
    r = H(tally())
    assert (r.status, r.company_verified, r.masters, r.ledger_count, r.read_only_available, r.posting) == \
        (Health.COMPANY_VERIFIED, True, "ok", 4, True, "disabled")
    assert r.companies_loaded == 2 and r.company_guid == TEST_CO[1]


def test_health_tally_closed(tally):
    fake = tally()
    fake.close()
    r = H(fake)
    assert r.status == Health.TALLY_UNREACHABLE and not r.tally_reachable and not r.read_only_available


def test_health_open_port_is_not_enough(tally):
    assert H(tally(companies=[])).status == Health.NO_COMPANY_LOADED
    assert H(tally(companies=[OTHER_CO])).status == Health.COMPANY_UNRESOLVED
    assert H(tally(), company_name=None).status == Health.COMPANY_NOT_CONFIGURED
    impostor = H(tally(companies=[(TEST_CO[0], OTHER_CO[1])]))
    assert impostor.status == Health.COMPANY_GUID_MISMATCH and not impostor.read_only_available


def test_health_masters_failure_is_reported(tally):
    fake = tally(ledgers={TEST_CO[1]: []})
    r = H(fake, posting_enabled=True, client_id="33333333-3333-4333-8333-333333333333")
    assert r.company_verified and r.masters.startswith("failed") and not r.read_only_available
    assert r.posting == "enabled_but_not_ready"


def test_health_posting_states(tally):
    fake = tally()
    client = "33333333-3333-4333-8333-333333333333"
    assert H(fake, posting_enabled=True, client_id=client).posting == "enabled"
    assert H(fake, posting_enabled=True, client_id=client, company_guid=None).posting == "enabled_but_not_ready"


def test_health_pairing_without_guid_reports_the_guid_to_record(tally):
    r = H(tally(), company_guid=None)
    assert r.status == Health.COMPANY_VERIFIED and r.company_guid == TEST_CO[1]


# ---------- mapping ----------


M = LedgerMasters(TEST_CO[1], frozenset({"Purchase - Steel", "Acme Steel", "ACME STEEL PVT"}))


@pytest.mark.parametrize("name,mapping,status,tally_name", [
    ("Purchase - Steel", None, LedgerStatus.EXISTS, "Purchase - Steel"),
    ("Steel purchases", {"Steel purchases": "Purchase - Steel"}, LedgerStatus.EXISTS, "Purchase - Steel"),
    ("Steel purchases", None, LedgerStatus.MISSING, None),
    ("purchase - steel", None, LedgerStatus.AMBIGUOUS, None),
    ("Steel purchases", {"Steel purchases": "Ghost"}, LedgerStatus.MISSING, None),
])
def test_resolve_ledger(name, mapping, status, tally_name):
    table = MappingTable.from_rows([{"kind": "ledger", "our_key": k, "erp_value": v}
                                    for k, v in (mapping or {}).items()])
    r = resolve_ledger(name, M, TEST_CO[1], table)
    assert (r.status, r.tally_name) == (status, tally_name)


def test_resolve_without_masters_or_with_another_companys_masters():
    assert resolve_ledger("x", None, TEST_CO[1]).status is LedgerStatus.MASTERS_UNAVAILABLE
    assert resolve_ledger("Acme Steel", M, OTHER_CO[1]).status is LedgerStatus.COMPANY_UNVERIFIED
    assert resolve_party("Acme Steel", M, "").status is LedgerStatus.COMPANY_UNVERIFIED


def test_resolve_party_with_gstin_mapping():
    table = MappingTable.from_rows([{"kind": "party", "our_key": "27AAPFU0939F1ZV", "erp_value": "Acme Steel"}])
    assert resolve_party("Acme Steel", M, TEST_CO[1], table, "27AAPFU0939F1ZV").status is LedgerStatus.EXISTS
    r = resolve_party("ACME STEEL PVT", M, TEST_CO[1], table, "27AAPFU0939F1ZV")
    assert r.status is LedgerStatus.AMBIGUOUS and "GSTIN mapping" in r.detail
    assert resolve_party("Acme Steel", M, TEST_CO[1], table, None).status is LedgerStatus.EXISTS


@pytest.mark.parametrize("rows", [
    [{"kind": "gst", "our_key": "a", "erp_value": "b"}],
    [{"kind": "ledger", "our_key": "", "erp_value": "b"}],
    [{"kind": "ledger", "our_key": "a", "erp_value": " b"}],
    [{"kind": "ledger", "our_key": "a", "erp_value": "b"}, {"kind": "ledger", "our_key": "a", "erp_value": "c"}],
])
def test_bad_mapping_rows_are_refused(rows):
    with pytest.raises(ValueError):
        MappingTable.from_rows(rows)


def test_masters_snapshot_payloads_match_person2_shape(tally):
    fake = tally()
    m = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port)).fetch_masters()
    company, ledgers = masters_snapshot_payloads(m)
    assert company == {"kind": "company", "items": [{"name": TEST_CO[0], "guid": TEST_CO[1]}]}
    assert ledgers["kind"] == "ledger" and {"name": "Cash", "parent": "Cash-in-Hand"} in ledgers["items"]
    with pytest.raises(ValueError):
        masters_snapshot_payloads(LedgerMasters(TEST_CO[1], frozenset({"Cash"})))


def test_masters_snapshot_payloads_validate_against_person2_models(tally):
    pytest.importorskip("fastapi")
    try:
        from finledger_platform.api import MastersIn, OpenBillsIn  # person2's own request models
    except Exception as exc:  # noqa: BLE001 - person2 deps (psycopg etc.) may be absent here
        pytest.skip(f"person2 package not importable here: {exc}")
    fake = tally()
    c = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port))
    for body in c.masters_snapshot():
        MastersIn.model_validate(body)
    fake.bills[TEST_CO[1]] = [bill("TEST-INV-001", "FinLedger Test Vendor", "1180.00", "1180.00")]
    OpenBillsIn.model_validate(c.fetch_open_bills().platform_payload())


# ---------- journal ----------


def test_journal_round_trip_and_gate(tmp_path):
    j = FileJournal(tmp_path)
    j.append(DOC, Phase.PREVIEW_READY, revision=1, idempotency_key=f"{DOC}:r1")
    assert gate(j.events(DOC)).status == "clear"
    j.append(DOC, Phase.SEND_STARTED, revision=1, idempotency_key=f"{DOC}:r1")
    assert gate(j.events(DOC)).status == "blocked"
    j.append(DOC, Phase.RESPONSE_RECEIVED, revision=1, idempotency_key=f"{DOC}:r1")
    j.append(DOC, Phase.SEND_NOT_DELIVERED, revision=1, idempotency_key=f"{DOC}:r1")
    assert gate(j.events(DOC)).status == "blocked"  # a reply was received: "not delivered" cannot follow it
    j.append(DOC, Phase.POSTED, revision=1, idempotency_key=f"{DOC}:r1", erp_id="g")
    j.append(DOC, Phase.HUMAN_CLEARED, revision=1, idempotency_key=f"{DOC}:r1", actor="x", detail="y")
    assert gate(j.events(DOC)).status == "posted"  # nothing un-posts


def test_journal_files_are_never_overwritten(tmp_path):
    j = FileJournal(tmp_path)
    j.save_bytes(DOC, "0001_request.xml", b"a")
    with pytest.raises(FileExistsError):
        j.save_bytes(DOC, "0001_request.xml", b"b")


@pytest.mark.parametrize("content", [b'{"seq":1', b"not json\n", b'{"seq": 2, "at": "", "phase": "posted", '
                                     b'"revision": 1, "idempotency_key": "k"}\n',
                                     b'{"seq": 1, "at": "", "phase": "made_up", "revision": 1, "idempotency_key": "k"}\n'])
def test_unreadable_journal_is_corrupt(tmp_path, content):
    (tmp_path / DOC).mkdir()
    (tmp_path / DOC / "journal.jsonl").write_bytes(content)
    with pytest.raises(JournalCorrupt):
        FileJournal(tmp_path).events(DOC)


@pytest.mark.parametrize("doc", ["../etc", "9D4F3D5D-499E-41DB-B510-E98348E5B503", "x" * 36, ""])
def test_journal_refuses_unsafe_document_ids(tmp_path, doc):
    with pytest.raises(ValueError):
        FileJournal(tmp_path).events(doc)
