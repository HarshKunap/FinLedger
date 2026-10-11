"""Read-only voucher lookup by FinLedger narration marker, for reconciliation. Never posts, never infers absence.

The request is the probe kit's ``voucher_export_request.xml`` (the form TallyPrime 7.1 EDU answered on 2026-10-10),
filled with the resolved company and a date window. The window is NOT trusted: the real capture proved
``SVFROMDATE``/``SVTODATE`` did not filter, and if a future Tally honours them a voucher whose date was edited could
fall outside. That is why ``not_found`` never means "never posted": callers treat it as "not seen in this reply".

Every outcome other than ``found`` blocks a resend (see posting.py). An oversized, malformed, rejected or unreachable
reply, or a request the pre-send guards refused, becomes ``unreadable`` with the reason, never ``not_found``. Rows are
never filtered by date here (``identity.find_by_marker`` ignores DATE), so a voucher whose date is far from ``around``
is still found. One call sends one pinned Export (plus the two company lists around it) and never retries.
"""

from __future__ import annotations

from datetime import date

from .company import pinned_export, xml_text
from .identity import (
    CompanyNotResolved,
    LookupResult,
    LookupStatus,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    find_by_marker,
    identity_marker,
    parse_voucher_export,
)
from .transport import RequestRefused, TallyTransport, TallyUnavailable

_VOUCHER_REQUEST = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>FinLedgerVoucherProbe</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
    <SVFROMDATE>{from_date}</SVFROMDATE>
    <SVTODATE>{to_date}</SVTODATE>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="FinLedgerVoucherProbe" ISMODIFY="No">
      <TYPE>Voucher</TYPE>
      <FETCH>GUID, MASTERID, ALTERID, DATE, VOUCHERTYPENAME, VOUCHERNUMBER, PARTYLEDGERNAME, REFERENCE, AMOUNT, NARRATION</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>
"""


def voucher_request(company: TallyCompany, from_date: date, to_date: date) -> bytes:
    if not isinstance(company, TallyCompany):
        raise TypeError("pin only a TallyCompany resolved from a fresh company list")
    if to_date < from_date:
        raise ValueError("to_date is before from_date")
    return _VOUCHER_REQUEST.format(company=xml_text(company.name), from_date=f"{from_date:%Y%m%d}",
                                   to_date=f"{to_date:%Y%m%d}").encode("utf-8")


def lookup_document(transport: TallyTransport, company_name: str, document_id: str, around: date,
                    expected_guid: str | None = None) -> tuple[LookupResult, TallyCompany | None]:
    """Find the one voucher of the verified company whose narration carries ``document_id``'s marker.

    Returns the classification and the company it was checked against (None when the company was not verified).
    Transport and parse failures become ``unreadable`` so a caller can never mistake them for absence. A bad
    ``document_id`` or an ``around`` that is not a date raises before any request.
    """
    identity_marker(document_id)  # refuse a bad id before any request
    if not isinstance(around, date):
        raise TypeError("around must be a date; it only fills the request's (unhonoured) date window")
    try:
        company, raw = pinned_export(transport, company_name, lambda c: voucher_request(c, around, around),
                                     expected_guid)
    except CompanyNotResolved as exc:
        return LookupResult(LookupStatus.UNREADABLE, f"company not verified: {exc}"), None
    except (TallyUnavailable, TallyMalformedResponse, TallyRequestRejected, RequestRefused) as exc:
        return LookupResult(LookupStatus.UNREADABLE, f"{type(exc).__name__}: {exc}"), None
    try:
        rows = parse_voucher_export(raw)
    except (TallyMalformedResponse, TallyRequestRejected) as exc:
        return LookupResult(LookupStatus.UNREADABLE, f"{type(exc).__name__}: {exc}"), company
    return find_by_marker(rows, document_id, company.guid), company
