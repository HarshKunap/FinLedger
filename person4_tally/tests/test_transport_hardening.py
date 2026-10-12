"""Transport hardening (mission Part 6): endpoint, timeouts, reply bounds, encodings, HTTP failure classification,
request guards, safe diagnostics, no retries, and "an open port is not health".

Every peer is in-process on an ephemeral 127.0.0.1 port: fake_tally.FakeTally, or the scripted ``Peer`` below for
HTTP framing a real server library would not produce. Nothing here talks to a real Tally.
"""

from __future__ import annotations

import dataclasses
import http.client
import logging
import socket
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    AgentSettings,
    Health,
    ImportOutcomeUnknown,
    LookupStatus,
    ReplyTooLarge,
    RequestRefused,
    TallyCompany,
    TallyConnector,
    TallyMalformedResponse,
    TallyRequestRejected,
    TallyTransport,
    TallyUnavailable,
    lookup_document,
    parse_company_list,
    post_approved,
)
from finledger_tally.bills import bill_request
from finledger_tally.company import COMPANY_LIST_REQUEST, list_companies
from finledger_tally.lookup import voucher_request
from finledger_tally.masters import ledger_request
from finledger_tally.transport import HOST, assert_export
from test_isolation_check import TEST_CO, company_list
from test_posting import NEW_DOC, approval, make_job, settings_for
from test_voucher_xml import preview

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = (FIXTURES / "preview" / "purchase_exempt_1180.xml").read_bytes()
GOLDEN_MULTILINE = (FIXTURES / "preview" / "purchase_exempt_multiline_1180.xml").read_bytes()
EXPORT = COMPANY_LIST_REQUEST
IMPORT = preview().xml
DAY = date(2026, 4, 1)
LIMIT = 1024
STATUS_0 = b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA></DATA></BODY></ENVELOPE>"
LINEERROR = (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>"
             b"<LINEERROR>Could not find Report &apos;X&apos;</LINEERROR></DATA></BODY></ENVELOPE>")
# Invoice and company text that must never reach a log line or an exception message.
SECRETS = ("Shree Ganesh Metals", "TEST-INV-001", "1180.00", "Test purchase", TEST_CO[0], "FinLedger Test Vendor")


# ---------- local peers ----------


class Peer:
    """A scripted HTTP peer on 127.0.0.1. It reads each whole request, then ``script(peer, conn)`` answers; a script
    returning True keeps the connection open (a stalled Tally). Counts connections, requests and bytes sent."""

    def __init__(self, script):
        self.script = script
        self.connections = 0
        self.finished = 0
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.sent = 0
        self.held: list[socket.socket] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        hold = False
        try:
            request = _read_request(conn)
            if request is not None:
                self.requests.append(request)
                hold = bool(self.script(self, conn))
        except OSError:
            pass
        finally:
            if hold:
                self.held.append(conn)
            else:
                conn.close()
            self.finished += 1

    def send(self, conn, data: bytes):
        conn.sendall(data)
        self.sent += len(data)

    def settle(self, seconds: float = 5.0):
        """Wait until every accepted connection has been served (so counts are final)."""
        deadline = time.monotonic() + seconds
        while self.finished < self.connections and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self):
        for conn in self.held:
            conn.close()
        self.sock.close()


def _read_request(conn) -> tuple[dict[str, str], bytes] | None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")[1:]
    headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines)}
    while len(body) < int(headers.get("content-length", "0")):
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return headers, body


def reply(status: int = 200, body: bytes = b"<ENVELOPE/>", headers=()):
    def script(peer, conn):
        head = [f"HTTP/1.1 {status} Whatever", *(f"{k}: {v}" for k, v in headers), f"Content-Length: {len(body)}",
                "Connection: close"]
        peer.send(conn, ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    return script


def raw(data: bytes, hold: bool = False):
    def script(peer, conn):
        if data:
            peer.send(conn, data)
        return hold
    return script


def framed(framing: str, body: bytes) -> bytes:
    if framing == "content-length":
        return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    if framing == "chunked":
        pieces = [body[i:i + 100] for i in range(0, len(body), 100)]
        return (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + b"".join(b"%X\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n")
    return b"HTTP/1.0 200 OK\r\n\r\n" + body  # no length: read until the peer closes


@pytest.fixture
def peer():
    made = []

    def start(script):
        p = Peer(script)
        made.append(p)
        return p

    yield start
    for p in made:
        p.close()


@pytest.fixture
def fake():
    f = FakeTally()
    yield f
    f.close()


@pytest.fixture(scope="module")
def _shared_fake():
    f = FakeTally()
    yield f
    f.close()


@pytest.fixture
def quiet_fake(_shared_fake):
    """A Tally that a refused request must never reach: one server for the module, its records cleared per test."""
    for record in (_shared_fake.server.received, _shared_fake.kinds, _shared_fake.imports):
        record.clear()
    return _shared_fake


@pytest.fixture
def dials(monkeypatch):
    """Every TCP connection attempt any code makes, as (address, timeout); http.client resolves
    ``socket.create_connection`` when a connection object is built, so this sees all of them."""
    calls = []
    real = socket.create_connection

    def record(address, timeout=None, *args, **kwargs):
        calls.append((address, timeout))
        return real(address, timeout, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", record)
    return calls


def T(port: int, **kw) -> TallyTransport:
    return TallyTransport(port=port, timeout=kw.pop("timeout", 2), **kw)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _nothing_sent(fake: FakeTally, dials: list) -> None:
    assert dials == [], "a connection was opened for a refused request"
    assert fake.server.received == [] and fake.kinds == [] and fake.imports == []


def _connector(port: int, timeout: float = 2) -> TallyConnector:
    return TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=port, timeout=timeout))


# ====================== 1. endpoint: 127.0.0.1, port only ======================


def test_every_connection_goes_to_loopback_on_the_configured_port_with_the_configured_timeout(fake, dials):
    t = TallyTransport(port=fake.port, timeout=3.5)
    t.export(EXPORT)
    t.import_vouchers(IMPORT)
    assert dials == [(("127.0.0.1", fake.port), 3.5)] * 2
    assert fake.kinds == ["Company", "import"]


@pytest.mark.parametrize("field", ["host", "url", "endpoint", "base_url", "proxy", "address"])
def test_no_endpoint_can_be_configured_on_the_transport(field):
    with pytest.raises(TypeError):
        TallyTransport(**{field: "203.0.113.5"})
    with pytest.raises(TypeError):
        dataclasses.replace(TallyTransport(), **{field: "203.0.113.5"})
    assert {f.name for f in dataclasses.fields(TallyTransport)} == {"port", "timeout", "max_reply_bytes"}
    assert HOST == "127.0.0.1"


def test_proxy_environment_variables_are_ignored(fake, peer, dials, monkeypatch):
    decoy = peer(reply())
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{decoy.port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    T(fake.port).export(EXPORT)
    T(fake.port).import_vouchers(IMPORT)
    assert decoy.connections == 0 and fake.kinds == ["Company", "import"]
    assert {address for address, _ in dials} == {("127.0.0.1", fake.port)}


def test_no_job_or_draft_field_can_choose_the_endpoint(fake, peer, dials, tmp_path):
    decoy = peer(reply())
    url = f"http://127.0.0.1:{decoy.port}/"
    j = make_job()
    j.update(tally_url=url, endpoint=url, host="203.0.113.5", port=decoy.port, tally_port=decoy.port)
    j["voucher_draft"].update(tally_url=url, host="203.0.113.5", port=decoy.port)
    post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert fake.kinds[:1] == ["Company"]  # the configured Tally was used ...
    assert decoy.connections == 0  # ... and nothing the job carried
    assert {address for address, _ in dials} == {("127.0.0.1", fake.port)}


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_never_followed(peer, status):
    decoy = peer(reply())
    p = peer(reply(status=status, body=b"", headers=[("Location", f"http://127.0.0.1:{decoy.port}/")]))
    with pytest.raises(TallyUnavailable, match=f"HTTP {status}"):
        T(p.port).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match=f"HTTP {status}"):
        T(p.port).import_vouchers(IMPORT)
    assert decoy.connections == 0 and p.connections == 2


# ====================== 2. connection and read timeouts ======================


@pytest.fixture
def full_backlog():
    """A listening port whose accept queue is full: on Linux a further connect gets no SYN-ACK and times out."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(0)
    port = srv.getsockname()[1]
    held = []
    try:
        for _ in range(8):
            probe = socket.socket()
            probe.settimeout(0.3)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                probe.close()
                break
            held.append(probe)
        else:
            pytest.skip("this platform completes connections beyond the listen backlog")
        yield port
    finally:
        for s in held:
            s.close()
        srv.close()


def test_a_connect_timeout_is_unavailable_and_an_import_counts_as_never_sent(full_backlog):
    started = time.monotonic()
    with pytest.raises(TallyUnavailable, match="did not accept a connection.*TimeoutError"):
        TallyTransport(port=full_backlog, timeout=0.4).export(EXPORT)
    # TallyUnavailable (not ImportOutcomeUnknown): the TCP handshake never completed, so no byte left this machine.
    with pytest.raises(TallyUnavailable, match="did not accept a connection"):
        TallyTransport(port=full_backlog, timeout=0.4).import_vouchers(IMPORT)
    assert time.monotonic() - started < 3


def test_a_connect_timeout_is_classified_the_same_without_relying_on_the_kernel(monkeypatch):
    seen = []

    def no_answer(address, timeout=None, *a, **k):
        seen.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", no_answer)
    with pytest.raises(TallyUnavailable, match="TimeoutError"):
        TallyTransport(port=9, timeout=1.5).export(EXPORT)
    with pytest.raises(TallyUnavailable, match="TimeoutError"):
        TallyTransport(port=9, timeout=1.5).import_vouchers(IMPORT)
    assert seen == [1.5, 1.5]


STALLS = {
    "before-status-line": b"",
    "mid-headers": b"HTTP/1.1 200 OK\r\n",
    "mid-body": b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n<ENVELOPE>",
    "mid-chunk": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n20\r\n<ENVELOPE>",
}


@pytest.mark.parametrize("sent", STALLS.values(), ids=STALLS.keys())
def test_a_stalled_reply_times_out_within_the_read_timeout(peer, sent):
    p = peer(raw(sent, hold=True))
    started = time.monotonic()
    with pytest.raises(TallyUnavailable, match="timed out"):
        T(p.port, timeout=0.4).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match="timed out"):
        T(p.port, timeout=0.4).import_vouchers(IMPORT)
    assert time.monotonic() - started < 4
    assert p.connections == 2


# ====================== 3. reply size bound ======================


def test_an_announced_oversize_reply_is_refused_without_reading_the_body(peer):
    p = peer(raw(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n", hold=True))  # the body never comes
    started = time.monotonic()
    with pytest.raises(ReplyTooLarge, match="announces 1048577"):
        T(p.port, timeout=5, max_reply_bytes=1024 * 1024).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match="ReplyTooLarge"):
        T(p.port, timeout=5, max_reply_bytes=1024 * 1024).import_vouchers(IMPORT)
    assert time.monotonic() - started < 3  # refused on the header, not after waiting 5 s for a body


@pytest.mark.parametrize("framing", ["content-length", "chunked", "until-close"])
def test_the_reply_bound_is_exact_for_every_framing(peer, framing):
    at_limit = b"x" * LIMIT
    assert T(peer(raw(framed(framing, at_limit))).port, max_reply_bytes=LIMIT).export(EXPORT) == at_limit
    over = peer(raw(framed(framing, at_limit + b"x")))
    with pytest.raises(ReplyTooLarge):
        T(over.port, max_reply_bytes=LIMIT).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match="ReplyTooLarge"):
        T(over.port, max_reply_bytes=LIMIT).import_vouchers(IMPORT)


def test_a_negative_chunk_size_cannot_stream_past_the_bound(peer):
    def flood(p, conn):
        p.send(conn, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n")
        block = b"x" * 65536
        for _ in range(1024):  # 64 MiB, unless the client stops reading
            p.send(conn, block)

    p = peer(flood)
    with pytest.raises(TallyUnavailable, match="IncompleteRead"):
        T(p.port, max_reply_bytes=LIMIT).export(EXPORT)
    p.settle()
    # plain http.client reads a "-1" chunk to the end of the stream: all 64 MiB, ignoring max_reply_bytes
    assert p.sent < 32 * 1024 * 1024
    with pytest.raises(ImportOutcomeUnknown, match="IncompleteRead"):
        T(p.port, max_reply_bytes=LIMIT).import_vouchers(IMPORT)


@pytest.mark.parametrize("size_line", [b"-1", b"-0", b"0x10", b"+10", b"1_0", b"zz", b"", b"1" * 17, b" -5 ;x"])
def test_a_chunk_size_that_is_not_plain_hex_is_an_incomplete_reply(peer, size_line):
    p = peer(raw(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + size_line
                 + b"\r\n<ENVELOPE/>\r\n0\r\n\r\n"))
    with pytest.raises(TallyUnavailable, match="IncompleteRead"):
        T(p.port).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match="IncompleteRead"):
        T(p.port).import_vouchers(IMPORT)


def test_a_chunked_reply_within_the_bound_is_read_whole(peer):
    pieces = [b"<ENVELOPE>", b"<HEADER><STATUS>1</STATUS></HEADER>", b"</ENVELOPE>"]
    stream = b"".join(b"%X;ext=1\r\n%s\r\n" % (len(p), p) for p in pieces)  # upper-case hex, chunk extensions
    p = peer(raw(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + stream + b"0\r\nX-Trailer: 1\r\n\r\n"))
    assert T(p.port).export(EXPORT) == b"".join(pieces)


# ====================== 4. encodings ======================


def test_requests_are_sent_as_their_exact_utf8_bytes_with_a_utf8_content_type(peer):
    body = ledger_request(TallyCompany('Café ₹ R&D <Imports> "Co"', TEST_CO[1]))
    p = peer(reply())
    T(p.port).export(body)
    headers, received = p.requests[0]
    assert received == body and received.decode("utf-8") and not received.startswith(b"\xef\xbb\xbf")
    assert headers["content-type"] == "text/xml; charset=utf-8" and headers["content-length"] == str(len(body))
    assert headers["host"] == f"127.0.0.1:{p.port}"


def _latin1(body: bytes) -> bytes:
    marker = b"<ID>FinLedgerCompanyProbe</ID>" if b"<ID>" in body else b"<REFERENCE>TEST-INV-001</REFERENCE>"
    return body.replace(marker, marker.replace(b"Probe", b"Caf\xe9").replace(b"INV", b"\xe9"))


BAD_REQUEST_ENCODINGS = {
    "utf-16 with BOM": lambda b: b.decode("utf-8").encode("utf-16"),
    "utf-16-le without BOM": lambda b: b.decode("utf-8").encode("utf-16-le"),
    "utf-8 with BOM": lambda b: b"\xef\xbb\xbf" + b,
    "latin-1 bytes": _latin1,
    "declared iso-8859-1": lambda b: b'<?xml version="1.0" encoding="ISO-8859-1"?>' + b,
    "declared utf-16": lambda b: b'<?xml version="1.0" encoding="UTF-16"?>' + b,
    "xml declaration not first": lambda b: b"\n" + b'<?xml version="1.0"?>' + b,
}


@pytest.mark.parametrize("encode", BAD_REQUEST_ENCODINGS.values(), ids=BAD_REQUEST_ENCODINGS.keys())
def test_requests_that_are_not_plain_utf8_are_refused_before_connecting(quiet_fake, dials, encode):
    for call, body in ((T(quiet_fake.port).export, EXPORT), (T(quiet_fake.port).import_vouchers, GOLDEN)):
        changed = encode(body)
        assert changed != body
        with pytest.raises(RequestRefused):
            call(changed)
    _nothing_sent(quiet_fake, dials)


@pytest.mark.parametrize("declaration", [b'<?xml version="1.0"?>', b'<?xml version="1.0" encoding="UTF-8"?>',
                                         b"<?xml version='1.0' encoding='utf-8' standalone='yes'?>"])
def test_a_utf8_xml_declaration_is_accepted(fake, declaration):
    assert parse_company_list(T(fake.port).export(declaration + b"\n" + EXPORT))
    assert T(fake.port).import_vouchers(declaration + b"\n" + GOLDEN).startswith(b"<RESPONSE><CREATED>1</CREATED>")


REPLY_ENCODINGS = {
    "utf-8": lambda text: text.encode("utf-8"),
    "utf-8 with BOM": lambda text: text.encode("utf-8-sig"),
    "utf-16-le with BOM": lambda text: b"\xff\xfe" + text.encode("utf-16-le"),
    "utf-16-be with BOM": lambda text: b"\xfe\xff" + text.encode("utf-16-be"),
}


@pytest.mark.parametrize("encode", REPLY_ENCODINGS.values(), ids=REPLY_ENCODINGS.keys())
def test_replies_decode_from_utf8_with_or_without_bom_and_utf16_with_bom(fake, encode):
    name = "Café ₹ Traders"
    fake.company_reply = encode(company_list([(name, TEST_CO[1])]).decode("utf-8"))
    assert list_companies(T(fake.port)) == [TallyCompany(name, TEST_CO[1])]


@pytest.mark.parametrize("reply_bytes", [
    b"<ENVELOPE>\xff\xfe\xfd</ENVELOPE>",  # not UTF-8, no BOM
    b"\xef\xbb\xbf<ENVELOPE>\xc3</ENVELOPE>",  # UTF-8 BOM, then a truncated sequence
    b"\xff\xfe<\x00E\x00N",  # UTF-16 BOM, odd byte count
    company_list([("Café ₹ Traders", TEST_CO[1])]).decode("utf-8").encode("utf-16-le"),  # UTF-16, no BOM
], ids=["invalid-utf8", "bom-then-invalid", "utf16-odd-length", "utf16-no-bom-non-ascii"])
def test_reply_bytes_that_do_not_decode_are_malformed_never_success(fake, reply_bytes):
    fake.company_reply = reply_bytes
    with pytest.raises(TallyMalformedResponse):
        list_companies(T(fake.port))
    report = _connector(fake.port).health()
    assert (report.status, report.company_verified, report.read_only_available) == (Health.MALFORMED_REPLY, False,
                                                                                     False)


# ====================== 5. malformed XML, Tally errors, unexpected envelopes ======================


UNREADABLE_REPLIES = {
    "empty": (b"", TallyMalformedResponse),
    "truncated-xml": (b"<ENVELOPE><HEADER><STATUS>1</STATUS>", TallyMalformedResponse),
    "html-error-page": (b"<html><body>Server Error</body></html>", TallyMalformedResponse),
    "response-root": (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", TallyMalformedResponse),
    "plain-text": (b"Tally is busy", TallyMalformedResponse),
    "two-collections": (b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><COLLECTION/><COLLECTION/>"
                        b"</DATA></BODY></ENVELOPE>", TallyMalformedResponse),
    "doctype": (b'<!DOCTYPE ENVELOPE [<!ENTITY a "x">]><ENVELOPE/>', TallyMalformedResponse),
    "status-0": (STATUS_0, TallyRequestRejected),
    "lineerror": (LINEERROR, TallyRequestRejected),
    "no-status": (b"<ENVELOPE><BODY><DATA><COLLECTION/></DATA></BODY></ENVELOPE>", TallyRequestRejected),
}


@pytest.mark.parametrize("reply_bytes,error", UNREADABLE_REPLIES.values(), ids=UNREADABLE_REPLIES.keys())
def test_unreadable_or_rejecting_replies_are_refused_and_never_healthy(fake, reply_bytes, error):
    fake.company_reply = reply_bytes
    with pytest.raises(error):
        list_companies(T(fake.port))
    report = _connector(fake.port).health()
    assert (report.status, report.company_verified, report.read_only_available) == (Health.MALFORMED_REPLY, False,
                                                                                     False)
    result, company = lookup_document(T(fake.port), TEST_CO[0], NEW_DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and company is None  # never "not found"


# ====================== 6. refused, closed early, non-200, broken HTTP ======================


def test_a_refused_connection_is_unavailable_for_both_calls_and_attempted_once(dials):
    port = _closed_port()
    with pytest.raises(TallyUnavailable, match="did not accept a connection"):
        T(port).export(EXPORT)
    with pytest.raises(TallyUnavailable, match="did not accept a connection"):
        T(port).import_vouchers(IMPORT)
    assert dials == [(("127.0.0.1", port), 2)] * 2


CLOSED_EARLY = {
    "before-status-line": b"",
    "after-status-line": b"HTTP/1.1 200 OK\r\n",
    "after-headers": b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n",
    "mid-body": b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n<ENVELOPE><HEADER>",
    "mid-chunk": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n32\r\n<ENVELOPE>",
    "before-last-chunk": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nA\r\n<ENVELOPE>\r\n",
}


@pytest.mark.parametrize("sent", CLOSED_EARLY.values(), ids=CLOSED_EARLY.keys())
def test_a_connection_closed_early_is_never_a_short_success(peer, sent):
    p = peer(raw(sent))
    with pytest.raises(TallyUnavailable):
        T(p.port).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown):
        T(p.port).import_vouchers(IMPORT)


def test_a_reply_cut_short_without_any_framing_is_still_refused_by_the_parser(peer):
    full = company_list([TEST_CO])
    p = peer(raw(b"HTTP/1.0 200 OK\r\n\r\n" + full[: len(full) // 2]))
    data = T(p.port).export(EXPORT)  # no Content-Length and no chunks: the transport cannot see the cut
    assert data == full[: len(full) // 2]
    with pytest.raises(TallyMalformedResponse):
        parse_company_list(data)


@pytest.mark.parametrize("status", [201, 204, 206, 304, 400, 404, 500, 503])
def test_any_status_but_200_is_unavailable_for_reads_and_unknown_for_imports(peer, status):
    p = peer(reply(status=status, body=b"" if status in (204, 304) else company_list([TEST_CO])))
    with pytest.raises(TallyUnavailable, match=f"HTTP {status}"):
        T(p.port).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown, match=f"HTTP {status}"):
        T(p.port).import_vouchers(IMPORT)


BROKEN_HTTP = {
    "not-http": b"FinLedger Test Company\r\n\r\n",
    "http-2-status": b"HTTP/2.0 200 OK\r\nContent-Length: 11\r\n\r\n<ENVELOPE/>",
    "status-not-a-number": b"HTTP/1.1 OK 200\r\n\r\n",
    "conflicting-content-lengths": b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nContent-Length: 4\r\n\r\n<ENVELOPE/>",
    "listed-content-lengths": b"HTTP/1.1 200 OK\r\nContent-Length: 11, 4\r\n\r\n<ENVELOPE/>",
    "superscript-content-length": "HTTP/1.1 200 OK\r\nContent-Length: ²\r\n\r\n<ENVELOPE/>".encode("latin-1"),
    "signed-content-length": b"HTTP/1.1 200 OK\r\nContent-Length: +11\r\n\r\n<ENVELOPE/>",
    "negative-content-length": b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n<ENVELOPE/>",
    "underscored-content-length": b"HTTP/1.1 200 OK\r\nContent-Length: 1_1\r\n\r\n<ENVELOPE/>",
    "too-many-headers": b"HTTP/1.1 200 OK\r\n" + b"X-A: b\r\n" * 101 + b"\r\n",
    "header-line-too-long": b"HTTP/1.1 200 OK\r\nX-A: " + b"a" * 70000 + b"\r\n\r\n",
}


@pytest.mark.parametrize("sent", BROKEN_HTTP.values(), ids=BROKEN_HTTP.keys())
def test_broken_http_framing_is_unavailable_for_reads_and_unknown_for_imports(peer, sent):
    p = peer(raw(sent))
    with pytest.raises(TallyUnavailable):
        T(p.port).export(EXPORT)
    with pytest.raises(ImportOutcomeUnknown):
        T(p.port).import_vouchers(IMPORT)


def test_a_repeated_identical_content_length_is_accepted(peer):
    p = peer(raw(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nContent-Length: 11\r\n\r\n<ENVELOPE/>"))
    assert T(p.port).export(EXPORT) == b"<ENVELOPE/>"


# ====================== 7. structured exceptions ======================


def test_the_failure_types_cannot_be_mistaken_for_each_other():
    # posting treats TallyUnavailable from import_vouchers as "never sent, resend allowed": an unknown outcome must
    # never be caught by that clause, and a refusal or an oversized reply is neither of them.
    assert issubclass(TallyUnavailable, ConnectionError) and issubclass(ImportOutcomeUnknown, ConnectionError)
    assert not issubclass(ImportOutcomeUnknown, TallyUnavailable)
    assert not issubclass(TallyUnavailable, ImportOutcomeUnknown)
    assert issubclass(ReplyTooLarge, TallyMalformedResponse) and not issubclass(ReplyTooLarge, ConnectionError)
    assert issubclass(RequestRefused, RuntimeError) and not issubclass(RequestRefused, (ConnectionError, ValueError))


def test_any_failure_after_an_import_connected_is_outcome_unknown(fake, monkeypatch):
    def broken(self, *a, **k):
        raise ValueError("unexpected internal state")

    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", broken)
    with pytest.raises(ImportOutcomeUnknown, match="ValueError"):
        T(fake.port).import_vouchers(IMPORT)
    deadline = time.monotonic() + 5
    while not fake.imports and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(fake.imports) == 1  # the Import did reach Tally: "unknown" is the only honest answer


# ====================== 8. safe diagnostics ======================


def test_logs_and_error_texts_carry_no_request_or_reply_content(peer, fake, caplog):
    caplog.set_level(logging.DEBUG)
    pinned = ledger_request(TallyCompany(SECRETS[0], TEST_CO[1]))
    texts = []
    echoing = [peer(raw(f"{SECRETS[0]} TEST-INV-001 1180.00\r\n\r\n".encode())),  # BadStatusLine repeats it
               peer(raw(f"HTTP/{SECRETS[0]}\r\n\r\n".encode())),  # UnknownProtocol repeats it
               peer(reply(status=500, body=SECRETS[0].encode()))]
    for p in echoing:
        for call, body, error in ((T(p.port).export, pinned, TallyUnavailable),
                                  (T(p.port).import_vouchers, IMPORT, ImportOutcomeUnknown)):
            with pytest.raises(error) as caught:
                call(body)
            texts.append(str(caught.value))
    with pytest.raises(ReplyTooLarge) as caught:
        T(peer(reply(body=b"x" * 2048)).port, max_reply_bytes=LIMIT).export(pinned)
    texts.append(str(caught.value))
    with pytest.raises(RequestRefused) as caught:
        T(fake.port).export(IMPORT)  # the refused body holds the company, party, reference and narration
    texts.append(str(caught.value))
    T(fake.port).export(pinned.replace(SECRETS[0].encode(), TEST_CO[0].encode()))
    T(fake.port).import_vouchers(IMPORT)
    logged = [r.getMessage() for r in caplog.records]
    assert any(r.name == "finledger_tally.transport" for r in caplog.records)  # diagnostics exist ...
    for text in texts + logged:  # ... and never carry invoice or company text
        assert len(text) <= 600
        assert not [s for s in SECRETS if s in text], text


def test_a_global_http_debug_level_cannot_print_request_bodies(fake, monkeypatch, capsys):
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 1)  # what a stray debugging aid would set
    T(fake.port).export(ledger_request(TallyCompany(*TEST_CO)))
    T(fake.port).import_vouchers(IMPORT)
    out = capsys.readouterr()
    assert not [s for s in SECRETS if s in out.out + out.err] and "send:" not in out.out
    assert fake.kinds == ["Ledger", "import"]


# ====================== 9. Export and Import are separate and guarded ======================


def test_export_and_import_are_separate_methods_that_refuse_each_other(quiet_fake, dials):
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).export(IMPORT)
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).import_vouchers(EXPORT)
    _nothing_sent(quiet_fake, dials)
    public = {n for n in dir(TallyTransport) if not n.startswith("_") and callable(getattr(TallyTransport, n))}
    assert public == {"export", "import_vouchers"}  # no ping or "is alive" that a bare TCP connect could satisfy


def _export(header: str = "", tdl: str = "") -> bytes:
    return ("<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
            f"<ID>Probe</ID>{header}</HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML"
            "</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE><COLLECTION NAME=\"Probe\" ISMODIFY=\"No\">"
            f"<TYPE>Company</TYPE><FETCH>NAME, GUID</FETCH></COLLECTION>{tdl}</TDLMESSAGE></TDL></DESC></BODY>"
            "</ENVELOPE>").encode("utf-8")


def test_the_smuggling_base_export_is_itself_acceptable(fake):
    assert parse_company_list(T(fake.port).export(_export())) and fake.kinds == ["Company"]


EXPORT_SMUGGLING = {
    "lowercase-tallymessage": _export(tdl="<tallymessage><voucher>x</voucher></tallymessage>"),
    "mixed-case-voucher-deep": _export(tdl="<COLLECTION NAME=\"Y\"><FETCH>X</FETCH><Voucher/></COLLECTION>"),
    "importdata-mixed-case": _export(tdl="<ImportData/>"),
    "import-element": _export(tdl="<IMPORT/>"),
    "name-containing-import": _export(tdl="<XIMPORTX/>"),
    "requestdata": _export(tdl="<REQUESTDATA/>"),
    "lowercase-action-attribute": _export(tdl="<OBJECT action=\"Create\"/>"),
    "mixed-case-action-on-collection": _export(tdl="<COLLECTION NAME=\"Y\" Action=\"Delete\"/>"),
    "action-element": _export(tdl="<ACTION>Create</ACTION>"),
    "tdl-function": _export(tdl="<FUNCTION NAME=\"F\"><VARIABLE>x</VARIABLE></FUNCTION>"),
    "ledger-master": _export(tdl="<LEDGER NAME=\"New\"><PARENT>Sundry Creditors</PARENT></LEDGER>"),
    "group-master-lowercase": _export(tdl="<group name=\"G\"/>"),
    "company-master": _export(tdl="<COMPANY NAME=\"C\"/>"),
    "second-request-lowercase": _export(header="<tallyrequest>Import Data</tallyrequest>"),
    "second-request-in-body": _export(tdl="<TALLYREQUEST>Import Data</TALLYREQUEST>"),
    "request-outside-header": _export().replace(b"<TALLYREQUEST>Export</TALLYREQUEST>", b"").replace(
        b"<BODY>", b"<BODY><TALLYREQUEST>Export</TALLYREQUEST>"),
    "kind-lowercase": _export().replace(b">Export<", b">export<"),
    "kind-padded": _export().replace(b">Export<", b"> Export <"),
    "kind-import": _export().replace(b">Export<", b">Import Data<"),
    "kind-execute": _export().replace(b">Export<", b">Execute<"),
    "two-company-pins": _export().replace(b"</STATICVARIABLES>", b"<SVCURRENTCOMPANY>A</SVCURRENTCOMPANY>"
                                          b"<svcurrentcompany>B</svcurrentcompany></STATICVARIABLES>"),
    "nested-envelope": _export(tdl="<ENVELOPE/>"),
    "two-envelopes": _export() + _export(),
    "root-not-envelope": _export().replace(b"<ENVELOPE>", b"<envelope>").replace(b"</ENVELOPE>", b"</envelope>"),
    "comment": _export(tdl="<!-- <TALLYMESSAGE><VOUCHER ACTION=\"Create\"/></TALLYMESSAGE> -->"),
    "cdata": _export(tdl="<X><![CDATA[<TALLYMESSAGE><VOUCHER ACTION=\"Create\"/></TALLYMESSAGE>]]></X>"),
    "processing-instruction": _export(tdl="<?tally import?>"),
    "doctype": b"<!DOCTYPE ENVELOPE>" + _export(),
    "entity-expanding-to-markup": b"<!DOCTYPE ENVELOPE [<!ENTITY x \"&#60;VOUCHER/&#62;\">]>" + _export(tdl="&x;"),
    "namespaced-voucher": _export(tdl="<x:VOUCHER xmlns:x=\"urn:x\"/>"),
    "namespaced-anything": _export(tdl="<x:Y xmlns:x=\"urn:x\"/>"),
    "not-xml": b"Export",
    "empty": b"",
    "str-not-bytes": _export().decode("utf-8"),
}


@pytest.mark.parametrize("body", EXPORT_SMUGGLING.values(), ids=EXPORT_SMUGGLING.keys())
def test_export_refuses_any_write_or_smuggled_markup_and_sends_nothing(quiet_fake, dials, body):
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).export(body)
    _nothing_sent(quiet_fake, dials)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("tallyprime_*/request_*.xml")),
                         ids=lambda p: f"{p.parent.name[-15:]}/{p.name}")
def test_the_real_captured_export_requests_pass_the_guard(path):
    assert_export(path.read_bytes())


def test_every_export_this_package_builds_passes_even_for_awkward_company_names():
    company = TallyCompany("R&D <Imports> \"Voucher\" Co's Ledger", TEST_CO[1])  # write-like words as text only
    for body in (COMPANY_LIST_REQUEST, ledger_request(company), voucher_request(company, DAY, DAY),
                 bill_request(company)):
        assert_export(body)


def _variant(old: bytes, new: bytes) -> bytes:
    assert old in GOLDEN
    return GOLDEN.replace(old, new, 1)


_CREATE = b' ACTION="Create"'
IMPORT_SMUGGLING = {
    "alter": _variant(_CREATE, b' ACTION="Alter"'),
    "delete": _variant(_CREATE, b' ACTION="Delete"'),
    "cancel": _variant(_CREATE, b' ACTION="Cancel"'),
    "create-lowercase-value": _variant(_CREATE, b' ACTION="create"'),
    "create-lowercase-name": _variant(_CREATE, b' action="Create"'),
    "no-action": _variant(_CREATE, b""),
    "second-action-nested": _variant(b"<ALLLEDGERENTRIES.LIST>", b'<ALLLEDGERENTRIES.LIST ACTION="Delete">'),
    "lowercase-action-nested": _variant(b"<ALLLEDGERENTRIES.LIST>", b'<ALLLEDGERENTRIES.LIST action="Delete">'),
    "two-actions-on-voucher": _variant(_CREATE, _CREATE + b' Action="Alter"'),
    "namespaced-action": _variant(_CREATE, _CREATE + b' xmlns:t="urn:t" t:ACTION="Delete"'),
    "tagname-alter": _variant(_CREATE, _CREATE + b' TAGNAME="Voucher Number" TAGVALUE="1"'),
    "remoteid": _variant(_CREATE, _CREATE + b' REMOTEID="' + TEST_CO[1].encode() + b'-00000001"'),
    "vchkey": _variant(_CREATE, _CREATE + b' VCHKEY="x"'),
    "guid-element": _variant(b"<DATE>", b"<GUID>" + TEST_CO[1].encode() + b"-00000001</GUID><DATE>"),
    "masterid-element": _variant(b"<DATE>", b"<MASTERID>1</MASTERID><DATE>"),
    "ledger-beside-voucher": _variant(b"</VOUCHER>", b'</VOUCHER><LEDGER NAME="New"><PARENT>X</PARENT></LEDGER>'),
    "ledger-in-a-second-message-elsewhere": _variant(
        b"</REQUESTDATA>", b'</REQUESTDATA><TALLYMESSAGE><LEDGER NAME="New"/></TALLYMESSAGE>'),
    "ledger-inside-voucher": _variant(
        b"<DATE>", b'<LEDGER NAME="New"><PARENT>Sundry Creditors</PARENT></LEDGER><DATE>'),
    "lowercase-ledger-inside-voucher": _variant(b"<DATE>", b'<ledger name="New"/><DATE>'),
    "group-inside-ledger-entry": _variant(b"<ISPARTYLEDGER>No</ISPARTYLEDGER>",
                                          b'<ISPARTYLEDGER>No</ISPARTYLEDGER><GROUP NAME="G"/>'),
    "company-inside-voucher": _variant(b"<DATE>", b'<COMPANY NAME="X"/><DATE>'),
    "stock-item-inside-voucher": _variant(b"<DATE>", b'<STOCKITEM NAME="X"/><DATE>'),
    "two-vouchers-one-message": _variant(b"</VOUCHER>", b'</VOUCHER><VOUCHER VCHTYPE="Purchase"><DATE>20260401</DATE>'
                                                        b"</VOUCHER>"),
    "nested-voucher": _variant(b"<DATE>", b"<VOUCHER><DATE>20260401</DATE></VOUCHER><DATE>"),
    "second-tallymessage": _variant(b"</TALLYMESSAGE>", b"</TALLYMESSAGE><TALLYMESSAGE/>"),
    "lowercase-tallymessage-inside-voucher": _variant(b"<DATE>", b"<tallymessage/><DATE>"),
    "second-requestdata": _variant(b"</IMPORTDATA>", b"<REQUESTDATA/></IMPORTDATA>"),
    "nested-object-not-a-list": _variant(b"<DATE>", b"<PARTY><NAME>x</NAME></PARTY><DATE>"),
    "report-all-masters": _variant(b"<REPORTNAME>Vouchers</REPORTNAME>", b"<REPORTNAME>All Masters</REPORTNAME>"),
    "report-lowercase": _variant(b"<REPORTNAME>Vouchers</REPORTNAME>", b"<REPORTNAME>vouchers</REPORTNAME>"),
    "company-blank": _variant(b">FinLedger Test Company<", b">   <"),
    "company-padded": _variant(b">FinLedger Test Company<", b"> FinLedger Test Company <"),
    "company-missing": _variant(b"<SVCURRENTCOMPANY>FinLedger Test Company</SVCURRENTCOMPANY>", b""),
    "second-company-pin": _variant(b"</STATICVARIABLES>", b"<svcurrentcompany>Other Existing Company"
                                                         b"</svcurrentcompany></STATICVARIABLES>"),
    "company-pin-elsewhere": _variant(b"<DATE>", b"<SVCURRENTCOMPANY>Other</SVCURRENTCOMPANY><DATE>"),
    "tdl-in-import": _variant(b"</REQUESTDESC>", b"<TDL><TDLMESSAGE/></TDL></REQUESTDESC>"),
    "second-request-kind": _variant(b"</HEADER>", b"<tallyrequest>Export</tallyrequest></HEADER>"),
    "kind-import-only": _variant(b">Import Data<", b">Import<"),
    "comment": _variant(b"<DATE>", b"<!-- x --><DATE>"),
    "cdata": _variant(b">TEST-INV-001</REFERENCE>", b"><![CDATA[TEST-INV-001]]></REFERENCE>"),
    "processing-instruction": _variant(b"<DATE>", b"<?tally x?><DATE>"),
    "doctype": b"<!DOCTYPE ENVELOPE>" + GOLDEN,
    "an-export": EXPORT,
    "bare-envelope": b"<ENVELOPE/>",
}


@pytest.mark.parametrize("body", IMPORT_SMUGGLING.values(), ids=IMPORT_SMUGGLING.keys())
def test_import_refuses_anything_but_one_new_voucher_and_sends_nothing(quiet_fake, dials, body):
    with pytest.raises(RequestRefused):
        T(quiet_fake.port).import_vouchers(body)
    _nothing_sent(quiet_fake, dials)


@pytest.mark.parametrize("body", [GOLDEN, GOLDEN_MULTILINE, IMPORT], ids=["golden", "golden-multiline", "preview"])
def test_one_company_pinned_create_is_accepted_and_sent_once(fake, body):
    assert T(fake.port).import_vouchers(body).startswith(b"<RESPONSE><CREATED>1</CREATED>")
    assert fake.imports == [body] and fake.kinds == ["import"]


# ====================== 10. no retries ======================


FAILURES = {
    "hang-up": raw(b""),
    "silence": raw(b"", hold=True),
    "non-200": reply(status=503),
    "truncated": raw(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n<ENVELOPE>"),
    "oversize": reply(body=b"x" * 2048),
    "not-http": raw(b"garbage\r\n\r\n"),
}


@pytest.mark.parametrize("script", FAILURES.values(), ids=FAILURES.keys())
def test_a_failed_export_is_attempted_exactly_once(peer, script):
    p = peer(script)
    with pytest.raises((TallyUnavailable, ReplyTooLarge)):
        T(p.port, timeout=0.3, max_reply_bytes=LIMIT).export(EXPORT)
    p.settle()
    assert (p.connections, len(p.requests)) == (1, 1)


@pytest.mark.parametrize("script", FAILURES.values(), ids=FAILURES.keys())
def test_a_failed_import_is_attempted_exactly_once(peer, script):
    p = peer(script)
    with pytest.raises(ImportOutcomeUnknown):
        T(p.port, timeout=0.3, max_reply_bytes=LIMIT).import_vouchers(IMPORT)
    p.settle()
    assert (p.connections, len(p.requests)) == (1, 1)


def test_health_and_lookup_make_one_attempt_when_tally_fails(fake):
    fake.down = True  # every request is answered by a dropped connection
    report = _connector(fake.port).health()
    assert report.status == Health.TALLY_UNREACHABLE and len(fake.server.received) == 1
    result, _ = lookup_document(T(fake.port), TEST_CO[0], NEW_DOC, DAY, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and len(fake.server.received) == 2


# ====================== 11. an open port is not health ======================


NOT_TALLY = {
    "accepts-then-closes": raw(b""),
    "accepts-then-silent": raw(b"", hold=True),
    "http-200-empty": reply(body=b""),
    "http-200-not-xml": reply(body=b"OK"),
    "http-200-bare-envelope": reply(body=b"<ENVELOPE/>"),
    "http-200-status-0": reply(body=STATUS_0),
}


@pytest.mark.parametrize("script", NOT_TALLY.values(), ids=NOT_TALLY.keys())
def test_an_open_port_or_a_completed_connection_is_never_reported_healthy(peer, script):
    p = peer(script)
    connector = _connector(p.port, timeout=0.3)
    report = connector.health()
    assert p.connections >= 1  # TCP connected ...
    assert report.status in (Health.TALLY_UNREACHABLE, Health.MALFORMED_REPLY)  # ... and that proved nothing
    assert not report.company_verified and not report.read_only_available and report.posting == "disabled"
    with pytest.raises((TallyUnavailable, TallyMalformedResponse, TallyRequestRejected)):
        connector.connect()
