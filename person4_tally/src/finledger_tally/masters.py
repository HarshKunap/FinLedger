"""Read-only ledger masters fetch. Sends Export requests only, through ``company.pinned_export``.

``fetch_ledger_masters(company_name)`` = company list -> Ledger collection Export pinned to the resolved company ->
company list again (same name and GUID). Pinning is NOT trusted to scope the reply (the 2026-10-10 isolation run only
partially verified it), so every ledger GUID must be ``<company GUID>-<8 hex digits>`` of the resolved company, or the
whole reply is refused.

Verified against real Tally (TallyPrime EDU 7.1, tests/fixtures/tallyprime_7_1_edu_masters_20261011_013427): the
company request and reply; this exact Ledger request; the reply shape (UTF-8, no BOM, one ``BODY/DATA/COLLECTION``
after a ``DESC/CMPINFO`` block, ``LEDGER`` rows with a ``NAME`` attribute and NO direct ``NAME`` child, the name
repeated in ``LANGUAGENAME.LIST/NAME.LIST/NAME``, ``PARENT``, ``GUID``); ledger GUIDs as ``<company GUID>-<8 hex>``;
``&#4; Primary`` as the reserved parent, read as ``Primary`` in ``PARENT`` and nowhere else. Seen on one small test
company only (4 ledgers): aliases (extra ``NAME.LIST`` entries) were not present in the real reply, so their handling
is from Tally's documented object model and fails closed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .company import COMPANY_LIST_REQUEST, check_name, pinned_export, plain_reply_text, xml_text
from .identity import (
    _COMPANY_GUID,
    _VOUCHER_GUID,
    CompanyNotResolved,
    TallyCompany,
    TallyMalformedResponse,
    _decode,
    _envelope_text,
    _rows,
)
from .transport import DEFAULT_PORT, DEFAULT_TIMEOUT, TallyTransport, TallyUnavailable
from .transport import assert_export as _assert_export

__all__ = ["COMPANY_LIST_REQUEST", "LedgerMasters", "TallyUnavailable", "fetch_ledger_masters", "ledger_request",
           "parse_ledger_export"]

_RESERVED_PRIMARY = re.compile(r"(<PARENT\b[^>]*>)&#4; Primary(</PARENT>)")
# A start tag with its quoted attribute values (a value may hold '>'), and one quoted value.
_START_TAG = re.compile(r"<[A-Za-z_][^\s/>]*((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*/?>")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
# Comments, CDATA sections and processing instructions are skipped whole, so a quote inside one never pairs with a
# quote in the markup after it (which could hide that markup's start tags from the scan below).
_NOT_TAGS = r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>"
_SCAN = re.compile(f"{_NOT_TAGS}|{_START_TAG.pattern}", re.S)
_XML_DECLARATION = re.compile(r"\A<\?xml\s[^<>?]*\?>")
_OTHER_MARKUP = re.compile(r"<!--|<!\[CDATA\[|<\?")

_LEDGER_REQUEST = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>FinLedgerLedgerMasters</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="FinLedgerLedgerMasters" ISMODIFY="No">
      <TYPE>Ledger</TYPE>
      <FETCH>NAME, PARENT, GUID</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>
"""


@dataclass(frozen=True)
class LedgerMasters:
    """Ledger names fetched from ONE company; ``company_guid`` ties them to that company.

    ``parents`` holds ``(ledger name, parent group name)`` pairs sorted by ledger name; it is empty when the masters
    were not built by ``parse_ledger_export``.
    """

    company_guid: str
    ledgers: frozenset[str]
    parents: tuple[tuple[str, str], ...] = ()
    company_name: str | None = None
    aliases: tuple[tuple[str, str], ...] = ()  # (alias, ledger name): never used to match, only to refuse

    def parent_of(self, ledger: str) -> str | None:
        return dict(self.parents).get(ledger)


def ledger_request(company: TallyCompany) -> bytes:
    """The Ledger collection Export pinned to an already-resolved company."""
    if not isinstance(company, TallyCompany):
        raise TypeError("pin only a TallyCompany resolved from a fresh company list")
    check_name(company.name, "company name", TallyMalformedResponse)
    return _LEDGER_REQUEST.format(company=xml_text(company.name)).encode("utf-8")


def parse_ledger_export(raw: bytes, company: TallyCompany) -> LedgerMasters:
    """Ledgers in a Ledger collection Export reply, refused whole unless every row belongs to ``company``.

    Raises only ``TallyRequestRejected`` (STATUS not 1, LINEERROR), ``CompanyNotResolved`` (a foreign or unverified
    GUID) or ``TallyMalformedResponse`` (everything else) for a bad reply, so callers can catch exactly those. There is
    no valid empty reply: every Tally company has built-in ledgers (the real reply holds the reserved
    ``Profit & Loss A/c``), so zero rows means the read failed, never "no ledgers".
    """
    if not _COMPANY_GUID.fullmatch(company.guid):
        raise CompanyNotResolved(f"company GUID {company.guid!r} is not a verified lowercase 8-4-4-4-12 GUID")
    text = _RESERVED_PRIMARY.sub(r"\1Primary\2", _decode(raw))
    declaration = _XML_DECLARATION.match(text)
    if _OTHER_MARKUP.search(text, declaration.end() if declaration else 0):
        raise TallyMalformedResponse("ledger reply holds a comment, CDATA section or processing instruction; the real "
                                     "TallyPrime reply has none, and they could split or hide a name, so it is refused")
    _refuse_raw_breaks_in_attributes(text)
    rows = _ledger_rows(_envelope_text(text))
    if not rows:
        raise TallyMalformedResponse("ledger reply holds no ledgers; every Tally company has at least its built-in "
                                     "ledgers, so an empty reply is not trusted")
    parents: dict[str, str] = {}
    folded: dict[str, str] = {}
    guids: set[str] = set()
    aliases: list[tuple[str, str]] = []
    for row in rows:
        name = _ledger_name(row)
        parent = _leaf(row, "PARENT")
        if parent is None:
            raise TallyMalformedResponse(f"ledger {name!r} has no PARENT")
        plain_reply_text(parent, f"PARENT of ledger {name!r}")
        guid = (_leaf(row, "GUID") or "").strip().lower()
        match = _VOUCHER_GUID.fullmatch(guid)
        if not match:
            raise TallyMalformedResponse(f"ledger {name!r} GUID {guid!r} is not <company GUID>-<8 hex digits>")
        if match.group(1) != company.guid:
            raise CompanyNotResolved(f"ledger {name!r} belongs to company GUID {match.group(1)}, not "
                                     f"{company.guid} ({company.name!r}); the whole reply is refused")
        if guid in guids:
            raise TallyMalformedResponse(f"ledger GUID {guid} appears twice")
        guids.add(guid)
        key = name.casefold()
        if key in folded:
            raise TallyMalformedResponse(f"ledger name {name!r} appears twice (as {folded[key]!r}); Tally names are "
                                         "unique regardless of case")
        folded[key] = name
        parents[name] = parent
        aliases.extend((alias, name) for alias in _aliases(row, name))
    for alias, owner in aliases:
        other = folded.get(alias.casefold())
        if other is not None and other != owner:
            raise TallyMalformedResponse(f"alias {alias!r} of ledger {owner!r} collides with ledger {other!r}")
    seen_alias: dict[str, str] = {}
    for alias, owner in aliases:
        if seen_alias.setdefault(alias.casefold(), owner) != owner:
            raise TallyMalformedResponse(f"alias {alias!r} belongs to two ledgers")
    return LedgerMasters(company_guid=company.guid, ledgers=frozenset(parents),
                         parents=tuple(sorted(parents.items())), company_name=company.name,
                         aliases=tuple(sorted(aliases)))


def fetch_ledger_masters(company_name: str, *, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT,
                         expected_guid: str | None = None, transport: TallyTransport | None = None) -> LedgerMasters:
    """Read the ledger masters of the loaded company named exactly ``company_name``. Read-only; see module doc.

    ``expected_guid`` (the GUID recorded at pairing) makes a same-named different company fail. Safe to call again at
    any time to refresh: it never changes Tally.
    """
    if not isinstance(company_name, str):
        raise TypeError("company_name must be a string")
    check_name(company_name, "company name")
    transport = transport or TallyTransport(port=port, timeout=timeout)
    company, raw = pinned_export(transport, company_name, ledger_request, expected_guid)
    return parse_ledger_export(raw, company)


def _refuse_raw_breaks_in_attributes(text: str) -> None:
    """XML turns a raw tab, CR or LF inside an attribute value into a space, so ``NAME="Cash<LF>Book"`` would be read
    as a ledger called ``Cash Book`` that Tally does not have. Such a reply is refused instead (an escaped ``&#10;``
    survives parsing and is refused as a control character). Comments, CDATA sections and processing instructions are
    skipped whole, never scanned as markup, so an unbalanced quote inside one cannot hide the start tags after it."""
    for tag in _SCAN.finditer(text):
        if tag.group(1) is not None and any(ch in value for value in _QUOTED.findall(tag.group(1)) for ch in "\t\r\n"):
            raise TallyMalformedResponse("an attribute value holds a raw tab or line break, which XML would have "
                                         "silently turned into a space")


def _ledger_rows(root: ET.Element) -> list[ET.Element]:
    """The collection's LEDGER rows. An element named LEDGER in any letter case anywhere else (in DATA, in DESC,
    nested in a row) refuses the reply, since it would otherwise be skipped silently. The only exemption is the real
    reply's counter: a childless ``LEDGER`` directly under ``BODY/DESC/CMPINFO``, which is a number, not a row."""
    rows = _rows(root, "LEDGER")
    counters = {e for e in root.findall("BODY/DESC/CMPINFO/LEDGER") if not len(e)}
    stray = sum(1 for e in root.iter() if e.tag.upper() == "LEDGER" and e not in counters) - len(rows)
    if stray:
        raise TallyMalformedResponse(f"reply holds {stray} LEDGER element(s) outside the collection rows")
    return rows


def _aliases(row: ET.Element, name: str) -> list[str]:
    """Names in LANGUAGENAME.LIST/NAME.LIST: the ledger's own name must be among them; the others are aliases."""
    listed = [_whole_text(n, "NAME.LIST/NAME") for n in row.findall("LANGUAGENAME.LIST/NAME.LIST/NAME")]
    if not listed:
        return []
    if name not in listed:
        raise TallyMalformedResponse(f"ledger {name!r} is missing from its own NAME.LIST {listed!r}")
    for alias in listed:
        plain_reply_text(alias, f"alias of ledger {name!r}")
    if len(set(listed)) != len(listed):
        raise TallyMalformedResponse(f"ledger {name!r} lists a name twice in NAME.LIST")
    return [a for a in listed if a != name]


def _ledger_name(row: ET.Element) -> str:
    values = [row.get("NAME")] if row.get("NAME") is not None else []
    values += [_whole_text(child, "NAME") for child in row.findall("NAME")]
    if not values:
        raise TallyMalformedResponse("ledger row has no NAME")
    if len(set(values)) != 1:
        raise TallyMalformedResponse(f"ledger row NAME values disagree: {values!r}")
    plain_reply_text(values[0], "ledger NAME")
    return values[0]


def _whole_text(el: ET.Element, what: str) -> str:
    """All of ``el``'s text. A field holding a child element is refused: ``.text`` is only the text before the first
    child, so ``Cash<X/>Book`` (or the tail text after the child) would otherwise be silently cut to ``Cash``."""
    if len(el):
        raise TallyMalformedResponse(f"ledger {what} holds a child element; mixed content is refused, not truncated")
    return el.text or ""


def _leaf(row: ET.Element, tag: str) -> str | None:
    """The whole text of the one ``tag`` child of ``row``; None when absent. Refused when ``tag`` appears twice or
    holds a child element. Kept here (not ``identity._text``) so the ledger parser's guarantees do not move with it."""
    found = row.findall(tag)
    if len(found) > 1:
        raise TallyMalformedResponse(f"ledger {tag} appears {len(found)} times in one row")
    return _whole_text(found[0], tag) if found else None
