"""Read-only ledger masters fetch. The company list request and reply are REAL (TallyPrime EDU 7.1 capture); every
Ledger collection reply here is SYNTHETIC, built on the real reply's envelope, because no real ledger reply has been
captured yet. These tests prove the fetch's own behaviour, not how TallyPrime answers a Ledger export."""

from __future__ import annotations

import re
import socket
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from finledger_tally import (
    CompanyNotResolved,
    LedgerMasters,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    TallyUnavailable,
    fetch_ledger_masters,
    parse_ledger_export,
    preview_purchase_xml,
)
from finledger_tally import masters as masters_module
from finledger_tally.masters import COMPANY_LIST_REQUEST, _assert_export, ledger_request
from test_identity_probe_kit import KIT, _FakeTally
from test_isolation_check import OTHER_CO, REJECTED, TEST_CO, company_list
from test_voucher_xml import job

HERE = Path(__file__).parent
REAL = HERE / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
COMPANY = TallyCompany(*TEST_CO)
OTHER = TallyCompany(*OTHER_CO)
_REAL_VOUCHERS = (REAL / "response_02_day_before_edit.xml").read_text(encoding="utf-8")
_HEAD = _REAL_VOUCHERS[:_REAL_VOUCHERS.index("<DATA>")]
_TAIL = _REAL_VOUCHERS[_REAL_VOUCHERS.index("</DATA>"):]


def ledger(name: str, parent: str, company_guid: str = TEST_CO[1], n: int = 1, *, attr: bool = True,
           child: str | None = None, guid: str | None = None) -> str:
    head = f'<LEDGER NAME="{name}" RESERVEDNAME="">' if attr else "<LEDGER>"
    name_child = f'<NAME TYPE="String">{child}</NAME>' if child is not None else ""
    return (f"{head}<PARENT TYPE=\"String\">{parent}</PARENT>{name_child}"
            f"<GUID TYPE=\"String\">{guid or f'{company_guid}-{n:08x}'}</GUID></LEDGER>\r\n")


BOOK = [ledger("FinLedger Test Vendor", "Sundry Creditors", n=0x1a),
        ledger("FinLedger Test Purchase", "Purchase Accounts", n=0x1b),
        ledger("Cash", "Cash-in-Hand", n=0x1),
        ledger("Profit &amp; Loss A/c", "&#4; Primary", n=0x2)]
OTHER_BOOK = [ledger("Other Co Vendor", "Sundry Creditors", OTHER_CO[1], n=0x1a)]


def ledgers_reply(rows: list[str]) -> bytes:
    return (_HEAD + "<DATA>\r\n<COLLECTION>\r\n" + "".join(rows) + "</COLLECTION>\r\n" + _TAIL).encode("utf-8")


class Tally:
    """Synthetic Tally answering the company list (real shape) and pinned Ledger exports (assumed shape)."""

    def __init__(self, companies=(TEST_CO,), books=None, pin="honour", later_companies=None):
        self.companies, self.pin, self.later = list(companies), pin, later_companies
        self.books = books if books is not None else {TEST_CO[1]: BOOK, OTHER_CO[1]: OTHER_BOOK}
        self.company_calls = 0

    def __call__(self, body: bytes, n: int) -> bytes:
        text = body.decode()
        if "<TYPE>Company</TYPE>" in text:
            self.company_calls += 1
            return company_list(self.later if self.later and self.company_calls > 1 else self.companies)
        name = ET.fromstring(text).findtext(".//SVCURRENTCOMPANY")
        guid = next((g for nm, g in self.companies if nm == name), None)
        if self.pin == "ignore":
            return ledgers_reply([r for rows in self.books.values() for r in rows])
        if guid is None:
            return REJECTED
        rows = self.books.get(guid, [])
        return rows if isinstance(rows, bytes) else ledgers_reply(rows)


@pytest.fixture
def serve():
    servers = []

    def start(model):
        fake = _FakeTally(b"", responder=model)
        servers.append(fake)
        return fake

    yield start
    for fake in servers:
        fake.close()


# ---------- valid replies ----------


def test_valid_fetch_sends_three_exports_and_pins_only_the_resolved_company(serve):
    fake = serve(Tally(companies=[TEST_CO, OTHER_CO]))
    m = fetch_ledger_masters(TEST_CO[0], port=fake.port)
    assert m.company_guid == TEST_CO[1]
    assert m.ledgers == {"FinLedger Test Vendor", "FinLedger Test Purchase", "Cash", "Profit & Loss A/c"}
    assert m.parents == (("Cash", "Cash-in-Hand"), ("FinLedger Test Purchase", "Purchase Accounts"),
                         ("FinLedger Test Vendor", "Sundry Creditors"), ("Profit & Loss A/c", "Primary"))
    assert len(fake.received) == 3
    assert fake.received[0] == fake.received[2] == COMPANY_LIST_REQUEST
    assert [ET.fromstring(r).findtext(".//SVCURRENTCOMPANY") for r in fake.received] == [None, TEST_CO[0], None]
    for req in fake.received:
        assert [e.text for e in ET.fromstring(req).iter("TALLYREQUEST")] == ["Export"]
        assert not re.search(rb"import|action\s*=|<voucher\b|<tallymessage\b", req, re.IGNORECASE)


def test_company_request_is_the_one_real_tally_answered():
    assert COMPANY_LIST_REQUEST == (KIT / "company_list_request.xml").read_bytes()
    assert COMPANY_LIST_REQUEST == (REAL / "request_01_companies.xml").read_bytes()


def test_real_company_reply_resolves_through_the_fetch(serve):
    real = (REAL / "response_01_companies.xml").read_bytes()
    model = Tally()
    fake = serve(lambda body, n: real if b"<TYPE>Company</TYPE>" in body else model(body, n))
    assert fetch_ledger_masters("FinLedger Test Company", port=fake.port).company_guid == TEST_CO[1]


def test_fetched_masters_feed_the_preview(serve):
    fake = serve(Tally())
    m = fetch_ledger_masters(TEST_CO[0], port=fake.port)
    p = preview_purchase_xml(job(), company=COMPANY, expected_company_name=TEST_CO[0], masters=m)
    assert p.party_ledger == "FinLedger Test Vendor"


@pytest.mark.parametrize("attr,child", [(True, None), (False, "Cash"), (True, "Cash")])
def test_name_from_attribute_child_or_both(attr, child):
    m = parse_ledger_export(ledgers_reply([ledger("Cash", "Cash-in-Hand", attr=attr, child=child)]), COMPANY)
    assert m.ledgers == {"Cash"}


def test_row_order_does_not_change_the_result():
    assert parse_ledger_export(ledgers_reply(BOOK), COMPANY) == parse_ledger_export(ledgers_reply(BOOK[::-1]), COMPANY)


def test_utf16_reply_with_bom_is_read():
    raw = ledgers_reply(BOOK).decode().encode("utf-16")
    assert parse_ledger_export(raw, COMPANY) == parse_ledger_export(ledgers_reply(BOOK), COMPANY)


# ---------- empty and malformed replies ----------


def test_empty_reply_is_refused():
    with pytest.raises(TallyMalformedResponse, match="no ledgers"):
        parse_ledger_export(ledgers_reply([]), COMPANY)


@pytest.mark.parametrize("raw", [
    b"", b"not xml", b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER>",
    b"<RESPONSE>Unknown Request</RESPONSE>",
    b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA></DATA></BODY></ENVELOPE>",
    ledgers_reply([ledger("Cash", "Cash-in-Hand").replace("LEDGER", "GROUP")]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand", attr=False)]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand", child="Petty Cash")]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand").replace('<PARENT TYPE="String">Cash-in-Hand</PARENT>', "")]),
    ledgers_reply([ledger("Cash", "")]),
    ledgers_reply([re.sub(r"<GUID[^<]*</GUID>", "", ledger("Cash", "Cash-in-Hand"))]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand", guid=" ")]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand", guid="00000001")]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand", guid=f"{TEST_CO[1]}-1")]),
    ledgers_reply([ledger(" Cash", "Cash-in-Hand")]),
    ledgers_reply([ledger("Cash&#9;", "Cash-in-Hand")]),
    ledgers_reply([ledger("Cash", "&#4; Cash-in-Hand")]),
    ledgers_reply([ledger("&#4; Cash", "Primary")]),
    ledgers_reply([ledger("Cash", "Cash-in-Hand") + "<VOUCHER/>"]),
])
def test_malformed_replies_are_refused(raw):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(raw, COMPANY)


@pytest.mark.parametrize("raw", [
    REJECTED, _HEAD.replace("<STATUS>1</STATUS>", "<STATUS>0</STATUS>").encode() + b"<DATA></DATA></BODY></ENVELOPE>",
])
def test_rejected_replies_are_refused(raw):
    with pytest.raises(TallyRequestRejected):
        parse_ledger_export(raw, COMPANY)


# ---------- duplicates ----------


@pytest.mark.parametrize("rows", [
    [ledger("Cash", "Cash-in-Hand", n=1), ledger("Cash", "Cash-in-Hand", n=2)],
    [ledger("Cash", "Cash-in-Hand", n=1), ledger("CASH", "Cash-in-Hand", n=2)],
    [ledger("Cash", "Cash-in-Hand", n=1), ledger("Bank", "Bank Accounts", n=1)],
], ids=["same-name", "case-only", "same-guid"])
def test_duplicate_ledgers_are_refused(rows):
    with pytest.raises(TallyMalformedResponse, match="twice"):
        parse_ledger_export(ledgers_reply(rows), COMPANY)


# ---------- wrong-company data and company identity ----------


def test_a_ledger_from_another_company_refuses_the_whole_reply():
    with pytest.raises(CompanyNotResolved, match="whole reply"):
        parse_ledger_export(ledgers_reply(BOOK + OTHER_BOOK), COMPANY)


def test_tally_ignoring_the_pin_is_caught(serve):
    fake = serve(Tally(companies=[TEST_CO, OTHER_CO], pin="ignore"))
    with pytest.raises(CompanyNotResolved):
        fetch_ledger_masters(TEST_CO[0], port=fake.port)


def test_unverified_company_guid_is_refused():
    with pytest.raises(CompanyNotResolved):
        parse_ledger_export(ledgers_reply(BOOK), TallyCompany(TEST_CO[0], TEST_CO[1].upper()))


@pytest.mark.parametrize("companies,name", [
    ([OTHER_CO], TEST_CO[0]),                                   # not loaded
    ([TEST_CO, OTHER_CO], TEST_CO[0].lower()),                  # case differs
    ([TEST_CO, OTHER_CO], TEST_CO[0] + "."),                    # near miss
    ([TEST_CO, (TEST_CO[0], OTHER_CO[1])], TEST_CO[0]),         # two loaded with that name
    ([], TEST_CO[0]),                                           # nothing loaded
])
def test_unresolved_company_is_never_pinned(serve, companies, name):
    fake = serve(Tally(companies=companies))
    with pytest.raises(CompanyNotResolved):
        fetch_ledger_masters(name, port=fake.port)
    assert fake.received == [COMPANY_LIST_REQUEST]  # the missing-company pin crashed real Tally: never sent


def test_malformed_company_list_stops_before_any_pin(serve):
    bad = company_list([TEST_CO]).replace(TEST_CO[1].encode(), b"not-a-guid")
    fake = serve(lambda body, n: bad)
    with pytest.raises(TallyMalformedResponse):
        fetch_ledger_masters(TEST_CO[0], port=fake.port)
    assert len(fake.received) == 1


def test_company_swapped_during_fetch_is_refused(serve):
    swapped = [(TEST_CO[0], "00000000-1111-4222-8333-444444444444")]
    fake = serve(Tally(later_companies=swapped))
    with pytest.raises(CompanyNotResolved, match="changed"):
        fetch_ledger_masters(TEST_CO[0], port=fake.port)


def test_company_unloaded_during_fetch_is_refused(serve):
    fake = serve(Tally(companies=[TEST_CO, OTHER_CO], later_companies=[OTHER_CO]))
    with pytest.raises(CompanyNotResolved):
        fetch_ledger_masters(TEST_CO[0], port=fake.port)


def test_company_name_is_escaped_into_the_pin(serve):
    name = "R&D <Traders> Import"
    listed = company_list([("R&amp;D &lt;Traders&gt; Import", TEST_CO[1])])
    fake = serve(lambda body, n: listed if b"<TYPE>Company</TYPE>" in body else ledgers_reply(BOOK))
    assert fetch_ledger_masters(name, port=fake.port).company_guid == TEST_CO[1]
    assert b"<SVCURRENTCOMPANY>R&amp;D &lt;Traders&gt; Import</SVCURRENTCOMPANY>" in fake.received[1]


# ---------- connection failures ----------


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _RawServer:
    """Accepts connections and then either hangs up at once (as Tally did when it crashed) or never answers."""

    def __init__(self, hang_up: bool):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self.hang_up = hang_up
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self.hang_up:
                conn.recv(65536)
                conn.close()
            else:
                self.held.append(conn)

    def close(self):
        for c in self.held:
            c.close()
        self.sock.close()


def test_tally_not_running_is_unavailable():
    with pytest.raises(TallyUnavailable):
        fetch_ledger_masters(TEST_CO[0], port=_closed_port(), timeout=2)


@pytest.mark.parametrize("hang_up", [True, False], ids=["connection-closed", "timeout"])
def test_tally_hanging_up_or_silent_is_unavailable(hang_up):
    server = _RawServer(hang_up)
    try:
        with pytest.raises(TallyUnavailable):
            fetch_ledger_masters(TEST_CO[0], port=server.port, timeout=0.5)
    finally:
        server.close()


def test_connection_lost_on_the_pinned_request_accepts_nothing(serve):
    model = Tally()

    def responder(body, n):
        if n == 2:
            raise ConnectionResetError("simulated crash")
        return model(body, n)

    fake = serve(responder)
    with pytest.raises(TallyUnavailable):
        fetch_ledger_masters(TEST_CO[0], port=fake.port, timeout=2)


# ---------- input and request guards ----------


@pytest.mark.parametrize("name", ["", " FinLedger Test Company", "FinLedger\nTest Company"])
def test_bad_company_name_sends_nothing(serve, name):
    fake = serve(Tally())
    with pytest.raises(ValueError):
        fetch_ledger_masters(name, port=fake.port)
    assert fake.received == []


@pytest.mark.parametrize("port", [0, 65536, True, "9000", 9000.0])
def test_bad_port_is_refused(port):
    with pytest.raises(ValueError):
        fetch_ledger_masters(TEST_CO[0], port=port)


def test_ledger_request_only_pins_a_resolved_company():
    with pytest.raises(TypeError):
        ledger_request(TEST_CO[0])
    req = ledger_request(COMPANY)
    _assert_export(req)
    assert ET.fromstring(req).findtext(".//SVCURRENTCOMPANY") == TEST_CO[0]
    assert ET.fromstring(req).findtext(".//COLLECTION/TYPE") == "Ledger"
    assert ET.fromstring(req).find(".//COLLECTION").get("ISMODIFY") == "No"


@pytest.mark.parametrize("body", [
    b"<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER></ENVELOPE>",
    b"<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><IMPORTDATA/></BODY></ENVELOPE>",
    b"<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><X ACTION=\"Create\"/></BODY></ENVELOPE>",
    b"<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST><TALLYREQUEST>Export</TALLYREQUEST></HEADER></ENVELOPE>",
])
def test_send_guard_refuses_anything_but_one_export(body):
    with pytest.raises(RuntimeError):
        _assert_export(body)


def test_preview_xml_can_never_be_sent_by_the_fetch():
    from test_voucher_xml import preview
    with pytest.raises(RuntimeError):
        _assert_export(preview().xml)


def test_only_the_transport_opens_connections_and_only_to_loopback():
    from finledger_tally import transport
    src_dir = Path(transport.__file__).parent
    assert transport.HOST == "127.0.0.1"
    for path in src_dir.glob("*.py"):
        src = path.read_text()
        calls = re.findall(r"HTTPConnection\(([^,]+),", src)
        if path.name == "transport.py":
            assert set(calls) == {"HOST"}
        else:
            assert not calls and "http.client" not in src, path.name
        # The one exception: ``__main__.py sync`` sends masters/open-bills snapshots to person2 with urllib (never to
        # Tally, never from any other module, only through ``_send``). Everything else stays loopback-transport only.
        allowed = "requests|socket|subprocess" if path.name == "__main__.py" else "urllib|requests|socket|subprocess"
        assert not re.search(rf"^\s*(import|from)\s+({allowed})", src, re.MULTILINE), path.name
        if path.name == "__main__.py":
            assert src.count("urlopen") == 0 and src.count(".open(request") == 1  # one send site, in _send


def test_masters_module_never_imports():
    src = Path(masters_module.__file__).read_text()
    assert "Import Data" not in src and "ACTION=" not in src and "import_vouchers" not in src


def test_masters_built_by_hand_still_work_without_parents():
    assert LedgerMasters(TEST_CO[1], frozenset({"Cash"})).parents == ()
