"""Company identity for every Tally read: resolve, pin, re-check.

``pinned_export`` is the one way a company-scoped Export is sent:
1. the Company collection Export (byte-identical to the probe kit request that produced the real reply in
   tests/fixtures/tallyprime_7_1_edu_run_20261010_162316) and an exact, case-sensitive name match to ONE loaded company,
   with no other loaded company of the same name ignoring case or spacing (``identity.resolve_company``);
2. when the caller knows the company's GUID (recorded at pairing), the resolved GUID must equal it: a different
   company that happens to have the same name is refused;
3. the caller's request, built only from that resolved company and checked before sending: exactly one
   ``BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY`` holding exactly the resolved name, or ``RequestRefused`` and nothing
   is sent (a pin to a company that is not loaded crashed TallyPrime 7.1 EDU on 2026-10-10, so an unresolved name is
   never sent; nothing here loads or switches companies);
4. the Company collection Export again, which must resolve to the same name and GUID.

Limits of the GUID-prefix policy, used by every parser on top of this: real evidence shows a voucher GUID is
``<company GUID>-<8 hex digits>`` (one voucher, one company). That ledgers and other masters follow the same form is
assumed, not seen. The suffix's meaning is not interpreted. A GUID prefix proves which company created an object; it
does not prove that a reply holds every object of that company.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Callable

from .identity import (
    _COMPANY_GUID,
    CompanyNotResolved,
    TallyCompany,
    TallyMalformedResponse,
    parse_company_list,
    resolve_company,
)
from .transport import RequestRefused, TallyTransport

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

COMPANY_LIST_REQUEST = b"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>FinLedgerCompanyProbe</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="FinLedgerCompanyProbe" ISMODIFY="No">
      <TYPE>Company</TYPE>
      <FETCH>NAME, GUID, STARTINGFROM, BOOKSFROM</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>
"""


def xml_text(value: str) -> str:
    """Escape for XML text exactly as the probe kit's [System.Security.SecurityElement]::Escape does."""
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def check_name(value: str, what: str, error: type[Exception] = ValueError) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or _CONTROL.search(value):
        raise error(f"{what} {value!r} is empty, padded, or holds control characters")
    return value


def check_guid(guid: str | None) -> str | None:
    if guid is None:
        return None
    if not isinstance(guid, str) or not _COMPANY_GUID.fullmatch(guid):
        raise ValueError("expected company GUID must be a lowercase 8-4-4-4-12 hex GUID")
    return guid


def list_companies(transport: TallyTransport) -> list[TallyCompany]:
    return parse_company_list(transport.export(COMPANY_LIST_REQUEST))


def current_company(transport: TallyTransport, name: str, expected_guid: str | None = None) -> TallyCompany:
    """The one loaded company named exactly ``name`` (and, when given, with GUID ``expected_guid``). A
    ``CompanyNotResolved`` message holds only the configured name and GUID and counts, never another company's."""
    check_name(name, "company name")
    expected_guid = check_guid(expected_guid)
    company = resolve_company(list_companies(transport), name)
    if expected_guid is not None and company.guid != expected_guid:
        raise CompanyNotResolved(f"loaded company {name!r} does not have the paired GUID {expected_guid}; "
                                 "a different company with the same name is refused")
    return company


def assert_pinned_to(request: bytes, company: TallyCompany) -> bytes:
    """Refuse (``RequestRefused``, nothing sent) a request that is not pinned to exactly ``company``: it must hold one
    SVCURRENTCOMPANY element (names compared ignoring case, as the transport guard does), at
    ``BODY/DESC/STATICVARIABLES``, with no child elements or attributes, whose text is ``company.name`` exactly; and no
    attribute anywhere named SVCURRENTCOMPANY (how Tally would read either shape is unknown)."""
    try:
        root = ET.fromstring(request)
    except (ET.ParseError, TypeError, ValueError):
        raise RequestRefused("refusing to send: the pinned request is not well-formed XML") from None
    pins = [e for e in root.iter() if isinstance(e.tag, str) and e.tag.upper() == "SVCURRENTCOMPANY"]
    pin_attrs = [e for e in root.iter() if any(k.upper() == "SVCURRENTCOMPANY" for k in e.attrib)]
    if len(pins) != 1 or pin_attrs or root.find("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY") is not pins[0] \
            or len(pins[0]) != 0 or pins[0].attrib or pins[0].text != company.name:
        raise RequestRefused("refusing to send: the request is not pinned to exactly the company resolved from the "
                             "fresh company list")
    return request


def pinned_export(transport: TallyTransport, name: str, build: Callable[[TallyCompany], bytes],
                  expected_guid: str | None = None) -> tuple[TallyCompany, bytes]:
    """Resolve, send ``build(company)`` as an Export, re-resolve. Returns the company and the raw reply bytes.

    Raises ``CompanyNotResolved`` (before: nothing pinned was sent; after: the reply is discarded) when the company is
    not exactly one loaded, unambiguous company with the expected GUID, or changes between the two company lists;
    ``RequestRefused`` when ``build`` did not pin the resolved company (nothing sent).
    """
    before = current_company(transport, name, expected_guid)
    raw = transport.export(assert_pinned_to(build(before), before))
    after = current_company(transport, name, expected_guid)
    if after != before:
        raise CompanyNotResolved(f"company {name!r} changed GUID during the read; nothing is accepted")
    return before, raw


def plain_reply_text(value: str, what: str) -> str:
    """Names read back from Tally: refused when empty, padded or holding control characters."""
    return check_name(value, what, TallyMalformedResponse)
