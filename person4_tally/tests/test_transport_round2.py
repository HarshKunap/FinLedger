"""Transport round 2: the Export TYPE allow-list, whole-content guard values, and the whole-call deadline.

Every peer is in-process on an ephemeral 127.0.0.1 port (fake_tally.FakeTally or the scripted ``Peer`` from
test_transport_hardening). Nothing here talks to a real Tally.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from finledger_tally import ImportOutcomeUnknown, RequestRefused, TallyCompany, TallyTransport, TallyUnavailable
from finledger_tally import transport as transport_module
from finledger_tally.bills import bill_request
from finledger_tally.company import COMPANY_LIST_REQUEST
from finledger_tally.lookup import voucher_request
from finledger_tally.masters import ledger_request
from finledger_tally.transport import assert_export, assert_voucher_import
from test_isolation_check import TEST_CO
from test_transport_hardening import DAY, EXPORT, GOLDEN, IMPORT, T, _export, _nothing_sent
from test_transport_hardening import _shared_fake, dials, peer, quiet_fake  # noqa: F401 - fixtures

KIT = Path(__file__).resolve().parents[1] / "tally_identity_probe"
FIXTURES = Path(__file__).parent / "fixtures"
COMPANY = TallyCompany("Shree Imports & \"Voucher\" Co", TEST_CO[1])


def _package_exports() -> dict[str, bytes]:
    return {"company-list": COMPANY_LIST_REQUEST, "ledgers": ledger_request(COMPANY),
            "vouchers": voucher_request(COMPANY, DAY, DAY), "bills": bill_request(COMPANY)}


# ====================== 1. Export: only HEADER/TYPE Collection ======================


@pytest.mark.parametrize("name", list(_package_exports()))
def test_every_package_export_is_a_collection_export_and_passes(name):
    body = _package_exports()[name]
    assert b"<TYPE>Collection</TYPE>" in body
    assert_export(body)


KIT_AND_CAPTURED = sorted(KIT.glob("*_request.xml")) + sorted(FIXTURES.glob("tallyprime_*/request_*.xml"))


@pytest.mark.parametrize("path", KIT_AND_CAPTURED, ids=lambda p: f"{p.parent.name[-15:]}/{p.name}")
def test_every_kit_template_and_captured_request_passes(path):
    raw = path.read_bytes().replace(b"__COMPANY__", b"Shree Imports Pvt Ltd")
    assert_export(raw.replace(b"__FROMDATE__", b"20260401").replace(b"__TODATE__", b"20260401"))


_TYPE = b"<TYPE>Collection</TYPE><ID>"
EXPORT_TYPE_SMUGGLING = {
    "function": _export().replace(_TYPE, b"<TYPE>Function</TYPE><ID>"),
    "tdlaction": _export().replace(_TYPE, b"<TYPE>TDLAction</TYPE><ID>"),
    "data": _export().replace(_TYPE, b"<TYPE>Data</TYPE><ID>"),
    "object": _export().replace(_TYPE, b"<TYPE>Object</TYPE><ID>"),
    "lowercase-value": _export().replace(_TYPE, b"<TYPE>collection</TYPE><ID>"),
    "padded-value": _export().replace(_TYPE, b"<TYPE> Collection </TYPE><ID>"),
    "lowercase-tag": _export().replace(_TYPE, b"<type>Collection</type><ID>"),
    "missing": _export().replace(_TYPE, b"<ID>"),
    "second-type": _export(header="<TYPE>Function</TYPE>"),
    "second-type-lowercase": _export(header="<type>TDLAction</type>"),
    "child-then-tail": _export().replace(_TYPE, b"<TYPE>Collection<X/>Function</TYPE><ID>"),
    "second-header": _export().replace(b"</HEADER>", b"</HEADER><HEADER><TYPE>Function</TYPE></HEADER>"),
    "kind-with-child-then-tail": _export().replace(b">Export<", b">Export<X/>Import Data<"),
    "company-pin-with-child": _export().replace(b"</STATICVARIABLES>", b"<SVCURRENTCOMPANY>A<X>B</X>"
                                                b"</SVCURRENTCOMPANY></STATICVARIABLES>"),
}


@pytest.mark.parametrize("body", EXPORT_TYPE_SMUGGLING.values(), ids=EXPORT_TYPE_SMUGGLING.keys())
def test_export_refuses_any_type_but_collection_and_any_guard_value_with_children(quiet_fake, dials, body):
    assert body != _export()
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).export(body)
    _nothing_sent(quiet_fake, dials)


# ====================== 2. Import: compared values are the element's whole content ======================

_PIN = b"<SVCURRENTCOMPANY>FinLedger Test Company</SVCURRENTCOMPANY>"
IMPORT_CHILD_SMUGGLING = {
    "pin-child": GOLDEN.replace(_PIN, b"<SVCURRENTCOMPANY>FinLedger Test Company<X>Other</X></SVCURRENTCOMPANY>"),
    "pin-child-then-tail": GOLDEN.replace(_PIN, b"<SVCURRENTCOMPANY>FinLedger Test Company<X/>Other Co"
                                                b"</SVCURRENTCOMPANY>"),
    "pin-only-child": GOLDEN.replace(_PIN, b"<SVCURRENTCOMPANY><X>FinLedger Test Company</X></SVCURRENTCOMPANY>"),
    "report-child-then-tail": GOLDEN.replace(b"<REPORTNAME>Vouchers</REPORTNAME>",
                                             b"<REPORTNAME>Vouchers<X/>All Masters</REPORTNAME>"),
    "kind-child-then-tail": GOLDEN.replace(b">Import Data<", b">Import Data<X/>Export<"),
}


@pytest.mark.parametrize("body", IMPORT_CHILD_SMUGGLING.values(), ids=IMPORT_CHILD_SMUGGLING.keys())
def test_import_refuses_a_guard_value_holding_child_elements(quiet_fake, dials, body):
    assert body != GOLDEN
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).import_vouchers(body)
    _nothing_sent(quiet_fake, dials)


def test_the_plain_golden_import_still_passes():
    assert_voucher_import(GOLDEN)
    assert_voucher_import(IMPORT)


# ====================== 3. whole-call deadline ======================


@pytest.mark.parametrize("timeout,budget", [(0.2, 60.0), (1, 60.0), (6, 60.0), (30, 300.0), (600, 6000.0)])
def test_the_call_budget_is_ten_timeouts_and_never_under_a_minute(timeout, budget):
    assert TallyTransport(timeout=timeout).call_budget == budget


def _trickle(head: bytes, pause: float, count: int = 120):
    """Send ``head`` then one byte every ``pause`` seconds (never silent for a whole read timeout), then close: 6 s
    at 0.05 s, so without a deadline the call would end only when the peer gives up, well after the budget."""
    def script(p, conn):
        try:
            p.send(conn, head)
            for _ in range(count):
                time.sleep(pause)
                p.send(conn, b"x")
        except OSError:
            pass
    return script


@pytest.fixture
def short_budget(monkeypatch):
    monkeypatch.setattr(transport_module, "_MIN_CALL_BUDGET", 0.0, raising=False)  # budget = 10 x timeout


@pytest.mark.parametrize("head", [b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n", b"HTTP/1.1 200 OK\r\nX-A: "],
                         ids=["body", "headers"])
def test_a_trickling_peer_cannot_hold_an_export_past_the_deadline(peer, short_budget, head):
    p = peer(_trickle(head, 0.05))
    start = time.monotonic()
    with pytest.raises(TallyUnavailable) as caught:
        T(p.port, timeout=0.2).export(EXPORT)
    assert time.monotonic() - start < 2.0 + 1.5  # budget 2 s; the per-read timeout alone would never fire
    assert "deadline" in str(caught.value)
    assert len(p.requests) == 1


@pytest.mark.parametrize("head", [b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n", b"HTTP/1.1 200 OK\r\nX-A: "],
                         ids=["body", "headers"])
def test_a_trickling_peer_after_an_import_is_outcome_unknown_at_the_deadline(peer, short_budget, head):
    p = peer(_trickle(head, 0.05))
    start = time.monotonic()
    with pytest.raises(ImportOutcomeUnknown) as caught:
        T(p.port, timeout=0.2).import_vouchers(IMPORT)
    assert time.monotonic() - start < 2.0 + 1.5
    assert "may exist" in str(caught.value) and "deadline" in str(caught.value)
    assert len(p.requests) == 1  # sent once, never retried


def test_a_slow_reply_that_finishes_within_the_budget_is_read_whole(peer, short_budget):
    body = b"<ENVELOPE/>"

    def script(p, conn):
        p.send(conn, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
        for i in range(len(body)):
            time.sleep(0.05)  # 0.55 s in all: several per-read timeouts' worth of bytes, inside the 2 s budget
            p.send(conn, body[i:i + 1])
    p = peer(script)
    assert T(p.port, timeout=0.2).export(EXPORT) == body
