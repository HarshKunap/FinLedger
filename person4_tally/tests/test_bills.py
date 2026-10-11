"""Open bills: payables classification, precision, rejection, identity changes. Bill replies are SYNTHETIC (no real
Bill reply has been captured); the ledgers (and their parents) are the fake's BOOK, built on the real ledger shape."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fake_tally import FakeTally, bill
from finledger_tally import (
    AgentSettings,
    CompanyNotResolved,
    TallyConnector,
    TallyMalformedResponse,
    TallyRequestRejected,
    TallyTransport,
    TallyUnavailable,
    fetch_open_bills,
    parse_bill_export,
)
from test_identity_probe_kit import _FakeTally
from test_isolation_check import OTHER_CO, REJECTED, TEST_CO, company_list
from test_masters import BOOK, ledgers_reply
from test_reads import COMPANY, MASTERS


@pytest.fixture
def fake():
    f = FakeTally()
    yield f
    f.close()


def connector(fake):
    return TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port, timeout=3))


def test_only_creditor_bills_with_a_credit_balance_are_payables(fake):
    fake.bills[TEST_CO[1]] = [bill("INV-1", "FinLedger Test Vendor", "1180.00", "1180.00"),
                              bill("ADV-1", "FinLedger Test Vendor", "-500.00", "-500.00"),
                              bill("PAID-1", "FinLedger Test Vendor", "100.00", "0"),
                              bill("C-1", "Cash", "50.00", "50.00")]
    bills = connector(fake).fetch_open_bills()
    assert len(bills.bills) == 4  # everything read is kept
    payables, excluded = bills.classify()
    assert [b.bill_ref for b in payables] == ["INV-1"]
    reasons = {b.bill_ref: why for b, why in excluded}
    assert "not 'Sundry Creditors'" in reasons["C-1"] and "not a positive" in reasons["ADV-1"]
    assert "not a positive" in reasons["PAID-1"]
    assert [b["bill_ref"] for b in bills.platform_payload()["bills"]] == ["INV-1"]


def test_amounts_are_exact_decimals_and_sent_as_strings(fake):
    fake.bills[TEST_CO[1]] = [bill("INV-1", "FinLedger Test Vendor", "0.10", "0.20")]
    b = connector(fake).fetch_open_bills()
    assert b.bills[0].closing == Decimal("0.20") and str(b.bills[0].closing) == "0.20"
    row = b.platform_payload()["bills"][0]
    assert (row["amount"], row["pending_amount"], row["due_date"]) == ("0.10", "0.20", None)


@pytest.mark.parametrize("amount", ["1.005", "1e3", "NaN", "Infinity", "+1.00", "1180.00 Cr", "", "1234567890123.00"])
def test_amounts_are_never_rounded_or_guessed(amount):
    with pytest.raises(TallyMalformedResponse, match="not a plain amount"):
        parse_bill_export(ledgers_reply([bill("X", "FinLedger Test Vendor", "1.00", amount)]), COMPANY, MASTERS)


def test_a_bill_without_a_reference_is_refused():
    row = bill("X", "FinLedger Test Vendor", "1.00", "1.00").replace(' NAME="X"', "")
    with pytest.raises(TallyMalformedResponse, match="NAME"):
        parse_bill_export(ledgers_reply([row]), COMPANY, MASTERS)


def test_a_tally_error_reply_is_not_an_empty_result():
    with pytest.raises(TallyRequestRejected):
        parse_bill_export(REJECTED, COMPANY, MASTERS)


def test_malformed_and_wrong_encoding_replies_are_refused():
    for raw in (b"<ENVELOPE><BODY>", b"\xff\xfe\x00", ledgers_reply([]).replace(b"<STATUS>1", b"<STATUS>x")):
        with pytest.raises((TallyMalformedResponse, TallyRequestRejected)):
            parse_bill_export(raw, COMPANY, MASTERS)


def test_tally_down_is_an_error_not_an_empty_list(fake):
    fake.down = True
    with pytest.raises(TallyUnavailable):
        connector(fake).fetch_open_bills()


def test_company_changing_during_the_bills_read_refuses_everything():
    calls = {"company": 0}

    def respond(body, n):
        if b"<TYPE>Company</TYPE>" in body:
            calls["company"] += 1
            # masters: before/after; bills: before (same) then after (another company took the name)
            return company_list([TEST_CO] if calls["company"] < 4 else [(TEST_CO[0], OTHER_CO[1])])
        if b"<TYPE>Ledger</TYPE>" in body:
            return ledgers_reply(BOOK)
        return ledgers_reply([bill("INV-1", "FinLedger Test Vendor", "1.00", "1.00")])
    srv = _FakeTally(b"", responder=respond)
    try:
        with pytest.raises(CompanyNotResolved):
            fetch_open_bills(TEST_CO[0], expected_guid=TEST_CO[1], transport=TallyTransport(port=srv.port, timeout=3))
    finally:
        srv.close()


def test_bills_read_sends_exports_only(fake):
    fake.bills[TEST_CO[1]] = [bill("INV-1", "FinLedger Test Vendor", "1.00", "1.00")]
    connector(fake).fetch_open_bills()
    assert fake.imports == [] and "import" not in fake.kinds
