"""The only code that opens a connection to Tally. Loopback only; Export (read) and Import (write) are separate calls.

- ``HOST`` is fixed to ``127.0.0.1``; only the port is configurable. No URL is ever taken from a draft or a job, no
  proxy variable is read (``http.client`` ignores them) and a redirect is never followed (it is a non-200 reply).
- Every request body is checked before a connection is opened: UTF-8 without a byte-order mark (the encoding the
  ``Content-Type`` header declares), no DOCTYPE, comment, CDATA section or processing instruction (markup the guard
  and Tally could read differently), no XML namespaces; element and attribute names are compared ignoring case.
  ``export`` refuses anything but one read-only ``Export`` of ``HEADER/TYPE`` ``Collection`` (the only kind
  FinLedger's reads use; ``Function``, ``TDLAction``, ``Data`` and the rest are refused); ``import_vouchers`` refuses
  anything but one company-pinned ``Import Data`` / ``Vouchers`` request holding exactly one
  ``VOUCHER ACTION="Create"`` and nothing else that writes. Every value a guard compares (request kind, TYPE, report
  name, company pin) must be the element's whole content: an element holding child elements is refused, so text
  after a child cannot change what Tally reads.
- No automatic retries, for reads or writes: each call opens one connection and sends one request. A failed read is
  reported and the caller decides; a failed write is never re-sent by this module.
- Replies are bounded (``max_reply_bytes``): a larger announced or actual body raises ``ReplyTooLarge`` without being
  read in full. A body shorter than its ``Content-Length``, broken chunk framing, a malformed ``Content-Length`` or
  an unframed reply closed before its first body byte is an incomplete reply, never a short success. (An unframed
  reply cut later cannot be told apart from a whole one; the XML parsers refuse what is cut.)
- ``timeout`` bounds the connect, the send and each socket read. Each call also has a whole-call deadline
  (``call_budget``: ten times ``timeout``, at least 60 s) so a peer trickling bytes cannot hold a call open; passing
  it is reported exactly like a read timeout (``TallyUnavailable`` for a read, ``ImportOutcomeUnknown`` for an
  Import that connected).
- Exceptions: ``RequestRefused`` (nothing sent), ``TallyUnavailable`` (a read failed, or an Import never connected,
  so nothing was written), ``ImportOutcomeUnknown`` (an Import connected and then anything failed: the voucher may
  exist), ``ReplyTooLarge`` (a read reply over the bound; a ``TallyMalformedResponse``). Their messages carry the
  port, exception type names, statuses and counts only; this module's debug log carries the call and exception type
  only. Neither ever carries request or reply bytes.
- An open port or a completed connection is not health; ``TallyConnector.health`` decides that from Tally's replies.
"""

from __future__ import annotations

import http.client
import io
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .identity import TallyMalformedResponse

log = logging.getLogger("finledger_tally.transport")

HOST = "127.0.0.1"  # loopback only; Tally's port is never reached over a network
DEFAULT_PORT = 9000
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_REPLY = 64 * 1024 * 1024
# Whole-call deadline: ten silences of ``timeout`` (a reply that pauses that long ten times is not a working Tally),
# never under one minute, so a short ``timeout`` that suits connect and first byte cannot cut a large reply that is
# still arriving. On loopback a 64 MiB reply is copied in well under a second once Tally starts writing it.
_CALL_BUDGET_FACTOR = 10
_MIN_CALL_BUDGET = 60.0
# HEADER/TYPE values an Export may carry: every FinLedger read (company list, ledgers, vouchers, bills) is a
# Collection export. Function, TDLAction, Data and anything else can run or render more than a collection fetch.
_EXPORT_TYPES = {"Collection"}

# Element names (upper-cased) an Export may never hold; any name containing IMPORT is refused as well.
_WRITE_ELEMENTS = {"IMPORTDATA", "TALLYMESSAGE", "VOUCHER", "REQUESTDATA", "ACTION", "FUNCTION"}
# Tally master objects: neither call may carry one (the connector never creates or alters a ledger or a company).
_MASTER_ELEMENTS = {"LEDGER", "GROUP", "COMPANY", "COSTCENTRE", "COSTCATEGORY", "CURRENCY", "BUDGET", "VOUCHERTYPE",
                    "STOCKITEM", "STOCKGROUP", "STOCKCATEGORY", "UNIT", "GODOWN", "EMPLOYEE", "ATTENDANCETYPE",
                    "SCENARIO", "TAXUNIT"}
# Names that target an existing object (Alter/Delete by tag, remote or master ids); a Create never needs them.
_TARGET_NAMES = {"TAGNAME", "TAGVALUE", "REMOTEID", "GUID", "VCHKEY", "MASTERID", "ALTERID"}
_IMPORT_PATHS = (("IMPORTDATA", "BODY/IMPORTDATA"), ("REQUESTDESC", "BODY/IMPORTDATA/REQUESTDESC"),
                 ("REPORTNAME", "BODY/IMPORTDATA/REQUESTDESC/REPORTNAME"),
                 ("STATICVARIABLES", "BODY/IMPORTDATA/REQUESTDESC/STATICVARIABLES"),
                 ("SVCURRENTCOMPANY", "BODY/IMPORTDATA/REQUESTDESC/STATICVARIABLES/SVCURRENTCOMPANY"),
                 ("REQUESTDATA", "BODY/IMPORTDATA/REQUESTDATA"),
                 ("TALLYMESSAGE", "BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE"),
                 ("VOUCHER", "BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE/VOUCHER"))
_XML_DECLARATION = re.compile(rb"<\?xml\s+version\s*=\s*([\"'])1\.0\1(\s+encoding\s*=\s*([\"'])(?i:utf-8)\3)?"
                              rb"(\s+standalone\s*=\s*([\"'])(?:yes|no)\5)?\s*\?>")
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
_MAX_CHUNK_LINE = 65536


class TallyUnavailable(ConnectionError):
    """A read could not be completed (refused, timed out, closed, non-200). Nothing was read; nothing was written."""


class ImportOutcomeUnknown(ConnectionError):
    """An Import was sent, or may have been, and no complete reply came back. The voucher may exist. Never resend."""


class ReplyTooLarge(TallyMalformedResponse):
    """The reply was larger than the configured bound and was not read in full."""


class RequestRefused(RuntimeError):
    """The request body failed the pre-send check; nothing was sent."""


class _DeadlineReader(io.RawIOBase):
    """Reads the socket with each wait capped at the time left before the call's deadline; at the deadline it raises
    ``TimeoutError``, the same exception a read timeout raises."""

    def __init__(self, sock, deadline: float, timeout: float):
        super().__init__()
        # the socket's own unbuffered reader keeps the socket open until the reply is read, as http.client expects
        self._raw = sock.makefile("rb", buffering=0)
        self._sock, self._deadline, self._timeout = sock, deadline, timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        self._sock.settimeout(_wait(self._deadline, self._timeout))
        try:
            return self._raw.readinto(buffer)
        except TimeoutError:
            _wait(self._deadline, self._timeout)  # names the deadline when it is what cut this read short
            raise

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


class _DeadlineSocket:
    """The connected socket, with sends and every read bounded by the call's deadline as well as ``timeout``."""

    def __init__(self, sock, deadline: float, timeout: float):
        self._sock, self._deadline, self._timeout = sock, deadline, timeout

    def sendall(self, data) -> None:
        self._sock.settimeout(_wait(self._deadline, self._timeout))
        try:
            self._sock.sendall(data)
        except TimeoutError:
            _wait(self._deadline, self._timeout)
            raise

    def makefile(self, mode: str = "r", *args, **kwargs):
        if mode != "rb":
            raise ValueError("only binary reads are used")
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline, self._timeout))

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _wait(deadline: float, timeout: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("the whole-call deadline passed")
    return min(timeout, left)


class _BoundedResponse(http.client.HTTPResponse):
    """``http.client`` takes a chunk size of ``-1`` as "read to the end of the stream", past any bound. Only plain
    hexadecimal chunk sizes are accepted here; anything else is an incomplete reply."""

    def _read_next_chunk_size(self):
        line = self.fp.readline(_MAX_CHUNK_LINE + 1)
        size = line.split(b";", 1)[0].strip()
        if len(line) > _MAX_CHUNK_LINE or not _CHUNK_SIZE.fullmatch(size):
            self._close_conn()
            raise ValueError("invalid chunk size")  # http.client turns this into IncompleteRead
        return int(size, 16)


@dataclass(frozen=True)
class TallyTransport:
    """Explicit settings for every connection. ``timeout`` covers connect, send and each read; ``call_budget``
    bounds a whole call."""

    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT
    max_reply_bytes: int = DEFAULT_MAX_REPLY

    def __post_init__(self):
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 0 < self.timeout <= 600:
            raise ValueError("timeout must be a number of seconds in (0, 600]")
        if isinstance(self.max_reply_bytes, bool) or not isinstance(self.max_reply_bytes, int) \
                or not 1024 <= self.max_reply_bytes <= 512 * 1024 * 1024:
            raise ValueError("max_reply_bytes must be an integer from 1 KiB to 512 MiB")

    @property
    def call_budget(self) -> float:
        """Seconds one call may take from start to last reply byte, whatever the peer does."""
        return max(_CALL_BUDGET_FACTOR * self.timeout, _MIN_CALL_BUDGET)

    def export(self, body: bytes) -> bytes:
        """Send one read-only Export and return the raw reply bytes. One attempt; any failure raises."""
        assert_export(body)
        conn = self._connect(time.monotonic() + self.call_budget)
        try:
            data = self._exchange(body, conn)
        except (OSError, http.client.HTTPException) as exc:
            log.debug("tally export failed: %s", type(exc).__name__)
            raise TallyUnavailable(f"Tally did not answer on {HOST}:{self.port}: {_describe(exc)}") from None
        except ReplyTooLarge:
            log.debug("tally export failed: ReplyTooLarge")
            raise
        log.debug("tally export answered")
        return data

    def import_vouchers(self, body: bytes) -> bytes:
        """Send one voucher Import. Any failure after connecting raises ``ImportOutcomeUnknown``; never retried."""
        assert_voucher_import(body)
        # TallyUnavailable from _connect means the TCP connection never opened: nothing was sent
        conn = self._connect(time.monotonic() + self.call_budget)
        try:
            data = self._exchange(body, conn)
        except Exception as exc:  # noqa: BLE001 - once connected, any failure may follow a delivered Import
            log.debug("tally import outcome unknown: %s", type(exc).__name__)
            raise ImportOutcomeUnknown(f"Import sent to {HOST}:{self.port} without a complete reply "
                                       f"({_describe(exc)}); the voucher may exist") from None
        log.debug("tally import answered")
        return data

    def _connect(self, deadline: float) -> http.client.HTTPConnection:
        """One TCP connection to Tally's port, its sends and reads bounded by ``deadline`` (``time.monotonic``) as
        well as ``timeout``. Connecting proves nothing about Tally's health."""
        conn = http.client.HTTPConnection(HOST, self.port, timeout=self.timeout)
        conn.set_debuglevel(0)  # a debug level set elsewhere would print request bodies to stdout
        conn.response_class = _BoundedResponse
        try:
            conn.connect()
        except OSError as exc:
            conn.close()
            log.debug("tally connect failed: %s", type(exc).__name__)
            raise TallyUnavailable(f"Tally did not accept a connection on {HOST}:{self.port}: "
                                   f"{_describe(exc)}") from None
        conn.sock = _DeadlineSocket(conn.sock, deadline, self.timeout)
        return conn

    def _exchange(self, body: bytes, conn: http.client.HTTPConnection) -> bytes:
        try:
            conn.request("POST", "/", body=body, headers={"Content-Type": "text/xml; charset=utf-8"})
            resp = conn.getresponse()
            announced = _announced_length(resp)
            if announced is not None and announced > self.max_reply_bytes:
                raise ReplyTooLarge(f"reply announces {announced} bytes, above the {self.max_reply_bytes}-byte bound")
            data = resp.read(self.max_reply_bytes + 1)
            if len(data) > self.max_reply_bytes:
                raise ReplyTooLarge(f"reply is larger than {self.max_reply_bytes} bytes")
            if announced is not None and len(data) != announced:
                raise http.client.IncompleteRead(data, announced - len(data))
            if resp.status != 200:
                raise http.client.HTTPException(f"HTTP {resp.status}")
            if announced is None and not resp.chunked and not data:
                # no framing and not one byte before the close (http.client ends headers at EOF): nothing arrived
                raise http.client.IncompleteRead(data)
            return data
        finally:
            conn.close()


def _announced_length(resp: http.client.HTTPResponse) -> int | None:
    """The body length the reply commits to, or None (chunked, or read until close). A Content-Length that is not
    one plain number (repeated with different values, signed, non-ASCII digits) makes the framing unknowable."""
    values = {v.strip() for header in resp.headers.get_all("Content-Length") or () for v in header.split(",")}
    if values and not (len(values) == 1 and all(v.isascii() and v.isdigit() for v in values)):
        raise http.client.HTTPException("Content-Length is not one plain number")
    return resp.length  # http.client's own reading: None when chunked, 0 for 1xx/204/304


def _describe(exc: BaseException) -> str:
    """Type name, plus the message only for errors whose text cannot echo reply bytes (``BadStatusLine`` and
    ``UnknownProtocol`` repeat what the server sent)."""
    safe = isinstance(exc, (OSError, http.client.IncompleteRead, ReplyTooLarge)) \
        or type(exc) is http.client.HTTPException
    return f"{type(exc).__name__}: {exc}"[:200] if safe else type(exc).__name__


def _parse(body: bytes) -> ET.Element:
    """The request as Tally will read it: UTF-8 without a BOM, no markup a parser might skip, expand or read
    differently, no namespaces."""
    if not isinstance(body, (bytes, bytearray)):
        raise RequestRefused("request body must be bytes")
    body = bytes(body)
    if body.startswith(b"\xef\xbb\xbf"):
        raise RequestRefused("request starts with a byte-order mark; requests are sent as UTF-8 without one")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise RequestRefused("request is not UTF-8, the encoding its Content-Type declares") from None
    if b"\x00" in body:  # never legal in XML; also how UTF-16/32 without a BOM passes a UTF-8 check
        raise RequestRefused("request holds NUL bytes (UTF-16 or UTF-32 without a byte-order mark?)")
    declaration = _XML_DECLARATION.match(body)
    if b"<!" in body or b"<?" in body[declaration.end() if declaration else 0:]:
        raise RequestRefused("request holds a DOCTYPE, comment, CDATA section, processing instruction or a non-UTF-8 "
                             "XML declaration")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RequestRefused(f"request is not well-formed XML: {exc}") from None
    if any("{" in e.tag or any("{" in name for name in e.attrib) for e in root.iter()):
        raise RequestRefused("request uses XML namespaces")
    return root


def _whole_text(element: ET.Element | None) -> str | None:
    """The element's whole text content, or None when it is missing or holds child elements (text after a child is
    the child's tail, which ``findtext`` would silently drop)."""
    if element is None or len(element):
        return None
    return element.text or ""


def _names(root: ET.Element) -> list[str]:
    return [e.tag.upper() for e in root.iter()]


def _attributes(root: ET.Element, names: set[str]) -> list[tuple[ET.Element, str]]:
    """(element, value) for every attribute whose name, ignoring case, is in ``names``."""
    return [(e, value) for e in root.iter() for name, value in e.attrib.items() if name.upper() in names]


def _request_kind_problems(root: ET.Element, kind: str) -> list[str]:
    problems = [] if root.tag == "ENVELOPE" else ["root is not ENVELOPE"]
    names = _names(root)
    if names.count("ENVELOPE") != 1:
        problems.append(f"{names.count('ENVELOPE')} ENVELOPE elements")
    requests = [e for e in root.iter() if e.tag.upper() == "TALLYREQUEST"]
    if len(requests) != 1 or root.find("HEADER/TALLYREQUEST") is not requests[0] \
            or _whole_text(requests[0]) != kind:
        problems.append(f"{len(requests)} TALLYREQUEST elements, expected one HEADER/TALLYREQUEST {kind!r}")
    return problems


def _refuse(what: str, problems: list[str]) -> None:
    if problems:
        raise RequestRefused(f"refusing to send: not {what} ({'; '.join(problems)})"[:600])


def assert_export(body: bytes) -> None:
    """Refuse anything but one read-only Collection Export (checked on the parsed request, not by keyword)."""
    root = _parse(body)
    problems = _request_kind_problems(root, "Export")
    headers = [e for e in root if e.tag.upper() == "HEADER"]
    types = [e for header in headers for e in header if e.tag.upper() == "TYPE"]
    if len(headers) != 1 or len(types) != 1 or types[0].tag != "TYPE" or _whole_text(types[0]) not in _EXPORT_TYPES:
        problems.append(f"expected one HEADER holding one TYPE from {sorted(_EXPORT_TYPES)}")
    names = _names(root)
    writes = sorted({n[:40] for n in names if n in _WRITE_ELEMENTS or n in _MASTER_ELEMENTS or "IMPORT" in n})
    if writes:
        problems.append(f"write or master elements {writes}")
    if _attributes(root, {"ACTION"}):
        problems.append("ACTION attributes")
    if names.count("SVCURRENTCOMPANY") > 1:
        problems.append("more than one SVCURRENTCOMPANY")
    if any(e.tag.upper() == "SVCURRENTCOMPANY" and _whole_text(e) is None for e in root.iter()):
        problems.append("SVCURRENTCOMPANY holds child elements")
    _refuse("a single read-only Export", problems)


def assert_voucher_import(body: bytes) -> None:
    """Refuse anything but one company-pinned Import of exactly one new voucher, holding no master, no reference to
    an existing object and no other write."""
    root = _parse(body)
    problems = _request_kind_problems(root, "Import Data")
    names = _names(root)
    for name, path in _IMPORT_PATHS:
        if names.count(name) != 1 or root.find(path) is None:
            problems.append(f"{names.count(name)} {name} elements, expected one at {path}")
    if _whole_text(root.find("BODY/IMPORTDATA/REQUESTDESC/REPORTNAME")) != "Vouchers":
        problems.append("REPORTNAME is not exactly 'Vouchers'")
    company = _whole_text(root.find("BODY/IMPORTDATA/REQUESTDESC/STATICVARIABLES/SVCURRENTCOMPANY"))
    if company is None or not company.strip() or company != company.strip():
        problems.append("SVCURRENTCOMPANY is missing, blank, padded or holds child elements")
    message = root.find("BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE")
    voucher = root.find("BODY/IMPORTDATA/REQUESTDATA/TALLYMESSAGE/VOUCHER")
    if message is not None and len(message) != 1:
        problems.append(f"TALLYMESSAGE holds {len(message)} objects")
    actions = _attributes(root, {"ACTION"})
    if len(actions) != 1 or actions[0][0] is not voucher or voucher.get("ACTION") != "Create":
        problems.append(f"{len(actions)} ACTION attributes, expected only VOUCHER ACTION='Create'")
    forbidden = sorted(set(names) & (_MASTER_ELEMENTS | _TARGET_NAMES | {"TDL", "TDLMESSAGE", "FUNCTION", "ACTION"}))
    if forbidden:
        problems.append(f"master, target or TDL elements {forbidden}")
    if _attributes(root, _TARGET_NAMES):
        problems.append("attributes naming an existing object")
    if voucher is not None and any(len(e) and not e.tag.upper().endswith(".LIST") for e in voucher.iter()
                                   if e is not voucher):
        problems.append("VOUCHER holds a nested object that is not a .LIST")
    _refuse("exactly one company-pinned Import creating one voucher", problems)
