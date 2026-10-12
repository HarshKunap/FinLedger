"""The ledger parser against the REAL TallyPrime EDU 7.1 Ledger reply (fixtures/tallyprime_7_1_edu_masters_*).

Positive facts come from the real bytes. Negative cases are the real bytes with ONE targeted edit each, so the
refusals are proven against the real shape rather than an invented one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from finledger_tally import (
    CompanyNotResolved,
    LedgerStatus,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    parse_company_list,
    parse_ledger_export,
    resolve_company,
    resolve_ledger,
    resolve_party,
)
from finledger_tally.masters import COMPANY_LIST_REQUEST, ledger_request
from finledger_tally import TallyTransport, fetch_ledger_masters
from test_identity_probe_kit import _FakeTally

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_masters_20261011_013427"
CO = TallyCompany("FinLedger Test Company", "7a5f42d4-ec1d-4aab-9992-e2e217bcb773")
LEDGERS = (REAL / "response_02_ledgers.xml").read_bytes()
COMPANIES = (REAL / "response_01_companies.xml").read_bytes()


def test_the_requests_python_sends_are_the_real_captured_requests():
    assert (REAL / "request_01_companies.xml").read_bytes() == COMPANY_LIST_REQUEST
    assert (REAL / "request_02_ledgers.xml").read_bytes() == ledger_request(CO)
    assert (REAL / "request_03_companies_after.xml").read_bytes() == COMPANY_LIST_REQUEST


def test_real_company_list_before_and_after_resolve_to_the_same_company():
    before = resolve_company(parse_company_list(COMPANIES), CO.name)
    after = resolve_company(parse_company_list((REAL / "response_03_companies_after.xml").read_bytes()), CO.name)
    assert before == after == CO


def test_real_ledger_reply_parses_exactly():
    assert not LEDGERS.startswith(b"\xef\xbb\xbf") and b"\r\n" in LEDGERS and b"&#4; Primary" in LEDGERS
    m = parse_ledger_export(LEDGERS, CO)
    assert m.company_guid == CO.guid and m.company_name == CO.name
    assert m.parents == (("Cash", "Cash-in-Hand"), ("FinLedger Test Purchase", "Purchase Accounts"),
                         ("FinLedger Test Vendor", "Sundry Creditors"), ("Profit & Loss A/c", "Primary"))
    assert m.aliases == ()  # the real NAME.LIST held only each ledger's own name


def test_real_ledgers_resolve_for_the_test_voucher():
    m = parse_ledger_export(LEDGERS, CO)
    assert resolve_party("FinLedger Test Vendor", m, CO.guid).status is LedgerStatus.EXISTS
    assert resolve_ledger("FinLedger Test Purchase", m, CO.guid).status is LedgerStatus.EXISTS
    assert resolve_ledger("finledger test purchase", m, CO.guid).status is LedgerStatus.AMBIGUOUS
    assert resolve_ledger("Steel Purchase", m, CO.guid).status is LedgerStatus.MISSING


def test_fetch_over_the_wire_with_the_real_replies():
    def respond(body, n):
        return COMPANIES if b"<TYPE>Company</TYPE>" in body else LEDGERS
    fake = _FakeTally(b"", responder=respond)
    try:
        m = fetch_ledger_masters(CO.name, expected_guid=CO.guid, transport=TallyTransport(port=fake.port, timeout=5))
    finally:
        fake.close()
    assert len(m.ledgers) == 4 and fake.received[1] == ledger_request(CO)


def _edit(old: bytes, new: bytes, count: int = 1) -> bytes:
    assert LEDGERS.count(old) >= count
    return LEDGERS.replace(old, new, count)


@pytest.mark.parametrize("raw,error,why", [
    (_edit(b"7a5f42d4-ec1d-4aab-9992-e2e217bcb773-0000001f", b"0b1c2d3e-4f50-4a61-8b72-c83d94e5f601-0000001f"),
     CompanyNotResolved, "belongs to company GUID"),
    (_edit(b"-000000cf</GUID>", b"</GUID>"), TallyMalformedResponse, "is not <company GUID>"),
    (_edit(b"-000000cf</GUID>", b"-000000ce</GUID>"), TallyMalformedResponse, "appears twice"),
    (_edit(b'<LEDGER NAME="Cash"', b'<LEDGER NAME="finledger test vendor"').replace(
        b"<NAME>Cash</NAME>", b"<NAME>finledger test vendor</NAME>"), TallyMalformedResponse, "appears twice"),
    (_edit(b'<PARENT TYPE="String">Cash-in-Hand</PARENT>', b""), TallyMalformedResponse, "has no PARENT"),
    (_edit(b'<PARENT TYPE="String">Cash-in-Hand</PARENT>', b'<PARENT TYPE="String"></PARENT>'),
     TallyMalformedResponse, "PARENT"),
    (_edit(b"<NAME>Cash</NAME>", b"<NAME>Petty Cash</NAME>"), TallyMalformedResponse, "missing from its own NAME.LIST"),
    (_edit(b"<NAME>Cash</NAME>", b"<NAME>Cash</NAME><NAME>FinLedger Test Vendor</NAME>"),
     TallyMalformedResponse, "collides with ledger"),
    (_edit(b"<NAME>Cash</NAME>", b"<NAME>Cash</NAME><NAME>Cash</NAME>"), TallyMalformedResponse, "twice in NAME.LIST"),
    (_edit(b'<LEDGER NAME="Cash" RESERVEDNAME="">', b'<LEDGER RESERVEDNAME="">'), TallyMalformedResponse, "NAME"),
    (_edit(b"<STATUS>1</STATUS>", b"<STATUS>0</STATUS>"), TallyRequestRejected, "STATUS is '0'"),
    (LEDGERS[:len(LEDGERS) // 2], TallyMalformedResponse, "not well-formed"),
], ids=["foreign_guid", "guid_without_suffix", "duplicate_guid", "duplicate_name_by_case", "no_parent",
        "blank_parent", "name_not_in_name_list", "alias_collides", "name_listed_twice", "no_name", "status_0",
        "truncated"])
def test_one_edit_to_the_real_reply_is_refused_whole(raw, error, why):
    with pytest.raises(error, match=why):
        parse_ledger_export(raw, CO)


def test_an_alias_is_recorded_and_never_matched():
    raw = _edit(b"<NAME>FinLedger Test Vendor</NAME>", b"<NAME>FinLedger Test Vendor</NAME><NAME>FTV</NAME>")
    m = parse_ledger_export(raw, CO)
    assert m.aliases == (("FTV", "FinLedger Test Vendor"),) and "FTV" not in m.ledgers
    r = resolve_party("FTV", m, CO.guid)
    assert r.status is LedgerStatus.AMBIGUOUS and "alias" in r.detail and r.tally_name is None
    assert resolve_ledger("ftv", m, CO.guid).status is LedgerStatus.AMBIGUOUS


def test_an_empty_real_shaped_reply_is_not_trusted():
    start = LEDGERS.index(b"<LEDGER ")
    end = LEDGERS.rindex(b"</LEDGER>") + len(b"</LEDGER>")
    with pytest.raises(TallyMalformedResponse, match="no ledgers"):
        parse_ledger_export(LEDGERS[:start] + LEDGERS[end:], CO)


def test_the_wrong_company_is_refused_even_with_real_bytes():
    other = TallyCompany("FinLedger Test Company", "0b1c2d3e-4f50-4a61-8b72-c83d94e5f601")
    with pytest.raises(CompanyNotResolved):
        parse_ledger_export(LEDGERS, other)


# ---------- the purchase preview against the REAL ledger masters ----------


def test_preview_builds_the_test_voucher_from_real_masters_with_no_network(monkeypatch):
    import hashlib
    import socket

    from test_voucher_xml import GOLDEN, job, preview

    def no_network(*a, **k):
        raise AssertionError("preview opened a socket")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    masters = parse_ledger_export(LEDGERS, CO)
    p = preview(masters=masters)
    assert p.xml == GOLDEN.read_bytes() and p.sha256 == hashlib.sha256(p.xml).hexdigest()
    assert [line[0] for line in p.lines] == ["FinLedger Test Purchase"] and p.stage == "preview_ready"


def test_preview_refuses_an_alias_or_unknown_ledger_from_real_masters():
    from finledger_tally import DraftRejected
    from test_voucher_xml import job, preview

    raw = _edit(b"<NAME>FinLedger Test Purchase</NAME>",
                b"<NAME>FinLedger Test Purchase</NAME><NAME>Purchases</NAME>")
    masters = parse_ledger_export(raw, CO)
    for line_ledger, code in (("Purchases", "ambiguous_ledger"), ("Steel Purchase", "missing_ledger")):
        j = job(lines=[{"ledger": line_ledger, "amount": "1180.00", "is_debit": True, "cost_centre": None}])
        with pytest.raises(DraftRejected) as info:
            preview(j, masters=masters)
        assert code in info.value.codes
