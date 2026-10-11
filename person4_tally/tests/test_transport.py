"""Transport, settings and request guards. Synthetic local servers only."""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

from finledger_tally import AgentSettings, ImportOutcomeUnknown, ReplyTooLarge, RequestRefused, TallyTransport, \
    TallyUnavailable
from finledger_tally.transport import assert_export, assert_voucher_import
from test_masters import _RawServer, _closed_port
from test_voucher_xml import preview

GOLDEN = Path(__file__).parent / "fixtures" / "preview" / "purchase_exempt_1180.xml"
EXPORT = (Path(__file__).parent.parent / "tally_identity_probe" / "company_list_request.xml").read_bytes()


class _Server:
    def __init__(self, status=200, body=b"<ENVELOPE/>", length=None):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                self.send_header("Content-Length", str(length if length is not None else len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        s = _Server(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


# ---------- settings ----------


@pytest.mark.parametrize("kw", [{"port": 0}, {"port": 70000}, {"port": "9000"}, {"port": True}, {"timeout": 0},
                                {"timeout": -1}, {"timeout": 601}, {"timeout": "30"}, {"max_reply_bytes": 10},
                                {"max_reply_bytes": 10**10}])
def test_transport_settings_are_validated(kw):
    with pytest.raises(ValueError):
        TallyTransport(**kw)


def test_there_is_no_host_or_url_setting():
    assert "host" not in TallyTransport.__dataclass_fields__ and "url" not in TallyTransport.__dataclass_fields__
    assert not any("HOST" in k or "URL" in k for k in AgentSettings.__dataclass_fields__)
    s = AgentSettings.from_env({"FINLEDGER_TALLY_HOST": "203.0.113.5", "FINLEDGER_TALLY_URL": "http://evil:9000"})
    assert s.transport() == TallyTransport()


@pytest.mark.parametrize("value,enabled", [("enabled", True), ("Enabled", False), ("1", False), ("true", False),
                                           ("yes", False), ("", False), (None, False)])
def test_posting_turns_on_only_with_the_exact_word(value, enabled):
    env = {} if value is None else {"FINLEDGER_TALLY_POSTING": value}
    assert AgentSettings.from_env(env).posting_enabled is enabled


def test_settings_from_env_and_redaction():
    s = AgentSettings.from_env({"FINLEDGER_TALLY_PORT": "9001", "FINLEDGER_TALLY_COMPANY": "Acme Steel Pvt Ltd",
                                "FINLEDGER_TALLY_COMPANY_GUID": "7a5f42d4-ec1d-4aab-9992-e2e217bcb773",
                                "FINLEDGER_TALLY_CLIENT_ID": "33333333-3333-4333-8333-333333333333"})
    assert s.port == 9001 and s.company_name == "Acme Steel Pvt Ltd"
    assert "Acme" not in str(s.redacted()) and s.redacted()["company_guid_set"] is True


@pytest.mark.parametrize("env", [{"FINLEDGER_TALLY_PORT": "nine"}, {"FINLEDGER_TALLY_COMPANY_GUID": "ABC"},
                                 {"FINLEDGER_TALLY_CLIENT_ID": "not-a-uuid"}, {"FINLEDGER_TALLY_COMPANY": " padded"}])
def test_bad_settings_are_refused(env):
    with pytest.raises(ValueError):
        AgentSettings.from_env(env)


# ---------- reads ----------


def test_refused_connection_is_unavailable():
    with pytest.raises(TallyUnavailable):
        TallyTransport(port=_closed_port(), timeout=2).export(EXPORT)


@pytest.mark.parametrize("hang_up", [True, False])
def test_hang_up_or_silence_is_unavailable(hang_up):
    srv = _RawServer(hang_up)
    try:
        with pytest.raises(TallyUnavailable):
            TallyTransport(port=srv.port, timeout=0.5).export(EXPORT)
    finally:
        srv.close()


def test_non_200_is_unavailable(server):
    with pytest.raises(TallyUnavailable):
        TallyTransport(port=server(status=500).port).export(EXPORT)


def test_oversized_reply_is_refused_by_announced_length(server):
    s = server(body=b"x" * 10, length=10_000_000)
    with pytest.raises(ReplyTooLarge):
        TallyTransport(port=s.port, timeout=2, max_reply_bytes=1024 * 1024).export(EXPORT)


def test_oversized_reply_is_refused_without_a_length():
    s2 = _ChunkServer(b"x" * 5000)
    try:
        with pytest.raises(ReplyTooLarge):
            TallyTransport(port=s2.port, timeout=2, max_reply_bytes=1024).export(EXPORT)
    finally:
        s2.close()


class _ChunkServer:
    """Answers HTTP/1.0 with no Content-Length, then closes."""

    def __init__(self, body: bytes):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.body = body
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.recv(65536)
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\n\r\n" + self.body)
        conn.close()

    def close(self):
        self.sock.close()


# ---------- the request guards ----------


@pytest.mark.parametrize("body", [
    GOLDEN.read_bytes(),
    b"<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER></ENVELOPE>",
    b"<ENVELOPE><HEADER><TALLYREQUEST>Export</TALLYREQUEST></HEADER><BODY><X ACTION=\"Delete\"/></BODY></ENVELOPE>",
    b"<ENVELOPE><HEADER><TALLYREQUEST>Execute</TALLYREQUEST></HEADER></ENVELOPE>",
    b"not xml", "text".encode()[:0],
])
def test_reads_refuse_anything_but_one_export(body):
    with pytest.raises(RequestRefused):
        assert_export(body)


def test_reads_never_open_a_connection_for_a_refused_body():
    with pytest.raises(RequestRefused):
        TallyTransport(port=_closed_port()).export(GOLDEN.read_bytes())


def _variant(old: bytes, new: bytes) -> bytes:
    body = GOLDEN.read_bytes()
    assert old in body
    return body.replace(old, new, 1)


@pytest.mark.parametrize("body", [
    EXPORT,
    _variant(b'ACTION="Create"', b'ACTION="Alter"'),
    _variant(b'ACTION="Create"', b'ACTION="Delete"'),
    _variant(b"<REPORTNAME>Vouchers</REPORTNAME>", b"<REPORTNAME>All Masters</REPORTNAME>"),
    _variant(b"<TALLYMESSAGE>", b"<TALLYMESSAGE><LEDGER NAME=\"New\" ACTION=\"Create\"/>"),
    _variant(b"</TALLYMESSAGE>", b"</TALLYMESSAGE><TALLYMESSAGE><VOUCHER ACTION=\"Create\"/></TALLYMESSAGE>"),
    _variant(b"<SVCURRENTCOMPANY>FinLedger Test Company</SVCURRENTCOMPANY>", b""),
    b"<ENVELOPE/>",
])
def test_imports_refuse_anything_but_one_new_voucher(body):
    with pytest.raises(RequestRefused):
        assert_voucher_import(body)


def test_the_preview_is_an_acceptable_import():
    assert_voucher_import(preview().xml)


# ---------- writes ----------


def test_import_refused_before_connecting_is_unavailable_not_unknown():
    with pytest.raises(TallyUnavailable):
        TallyTransport(port=_closed_port(), timeout=2).import_vouchers(preview().xml)


@pytest.mark.parametrize("hang_up", [True, False])
def test_import_without_a_complete_reply_is_unknown(hang_up):
    srv = _RawServer(hang_up)
    try:
        with pytest.raises(ImportOutcomeUnknown):
            TallyTransport(port=srv.port, timeout=0.5).import_vouchers(preview().xml)
    finally:
        srv.close()


def test_import_with_a_non_200_or_oversized_reply_is_unknown(server):
    with pytest.raises(ImportOutcomeUnknown):
        TallyTransport(port=server(status=500).port).import_vouchers(preview().xml)
    with pytest.raises(ImportOutcomeUnknown):
        TallyTransport(port=server(body=b"x", length=10**8).port, max_reply_bytes=1024).import_vouchers(preview().xml)
