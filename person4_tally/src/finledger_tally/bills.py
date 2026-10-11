"""Read-only outstanding (open) bills for the paired company. Never posts a payment; never invents due dates.

Request: a Bill collection Export pinned to the resolved company (company.pinned_export), fetching NAME, PARENT,
BILLDATE, OPENINGBALANCE, CLOSINGBALANCE. NOTHING about this reply has been seen from a real Tally: the collection type,
row tag (``BILL``), field names and amount format are taken from Tally's documented TDL object model and are parsed
strictly, so a different real shape fails closed. Bills carry no GUID that ties them to a company, so every bill's
party ledger must be an exact ledger of the same company's masters fetched in the same call, or the whole reply is
refused.

Amount sign: in the real voucher export a credited party line was positive (+1180.00, ISDEEMEDPOSITIVE No). Bills are
read with the same convention (positive = credit = payable), which is an assumption until a real bill reply is seen.
``due_date`` is always None: no verified Tally field maps to it.

Which bills are payables: a bill is sent to the platform only when its party ledger's parent group (from the same
call's masters) is exactly ``Sundry Creditors`` and its closing balance is positive. Everything else (debtors' bills,
creditors under a sub-group, zero or debit balances) is kept in ``OpenBills.excluded`` with a reason, never dropped
silently and never guessed. Groups are not fetched, so a vendor under a sub-group of Sundry Creditors is excluded,
not included: an incomplete ageing view is safer than a receivable shown as a payable. A bill whose closing balance is
a credit but whose opening balance is zero or a debit is excluded too: its bill amount (``amount`` in the payload)
cannot be stated without flipping a sign or inventing a total.

Parsing decisions (the reply shape is unverified, so each fails closed):
- Every ``BILL`` row is returned and then either sent or excluded with a reason; a ``BILL`` element anywhere outside
  the collection rows (nested in a row, beside the collection) refuses the reply instead of being skipped.
- Bill references are scoped to their party: the same reference under two parties is two bills (vendors number their
  invoices independently). The same party with the same reference twice, or with references that differ only in case,
  refuses the reply: summing or picking one would invent an amount, and a payment matched by reference must never be
  able to reach the wrong bill. "Differ only in case" is ``str.lower()`` equality, not full Unicode case folding (which
  would also merge e.g. ``MASS-1`` and ``Maß-1``).
- A field (``NAME``, ``PARENT``, ``BILLDATE``, the amounts) that holds a child element refuses the reply: only the
  text before the child would be read, so ``1<X/>180.00`` would silently become ``1``.
- Amounts and dates use ASCII digits only (a regex digit class and ``Decimal`` accept other scripts' digits).
  ``BILLDATE`` is strict ``YYYYMMDD`` and a real calendar date; an absent or blank ``BILLDATE`` is ``None`` (P2's
  ``bill_date`` is optional and an outstanding amount with no stated date is still owed), never a guessed date.
- A raw tab or line break inside an attribute value (the bill reference is the ``NAME`` attribute) refuses the reply,
  since XML would silently turn it into a space and change the reference.
- No date filter is sent or assumed: Tally ignored ``SVFROMDATE``/``SVTODATE`` for vouchers, so every bill returned is
  kept whatever its date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .company import check_name, pinned_export, plain_reply_text, xml_text
from .identity import (
    CompanyNotResolved,
    TallyCompany,
    TallyMalformedResponse,
    _decode,
    _envelope_text,
    _only_rows,
    _text,
)
from .masters import LedgerMasters, _refuse_raw_breaks_in_attributes, fetch_ledger_masters
from .transport import TallyTransport

PAYABLE_GROUP = "Sundry Creditors"
_AMOUNT = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,2})?")  # numeric(14,2); ASCII digits only
_DATE = re.compile(r"[0-9]{8}")

_BILL_REQUEST = """<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Collection</TYPE>
  <ID>FinLedgerOpenBills</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="FinLedgerOpenBills" ISMODIFY="No">
      <TYPE>Bill</TYPE>
      <FETCH>NAME, PARENT, BILLDATE, OPENINGBALANCE, CLOSINGBALANCE</FETCH>
     </COLLECTION>
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>
"""


@dataclass(frozen=True)
class OpenBill:
    party_ledger: str
    bill_ref: str
    bill_date: date | None
    opening: Decimal  # signed, Tally convention: positive = credit (payable)
    closing: Decimal  # signed outstanding

    @property
    def payable(self) -> bool:
        return self.closing > 0


@dataclass(frozen=True)
class OpenBills:
    company: TallyCompany
    bills: tuple[OpenBill, ...]  # () means Tally returned zero bills, not a failure
    as_of: datetime
    party_parents: tuple[tuple[str, str], ...] = ()  # (party ledger, parent group) from the same call's masters

    def classify(self) -> tuple[tuple[OpenBill, ...], tuple[tuple[OpenBill, str], ...]]:
        """(payables, excluded with reason). See the module doc for the rule."""
        parents = dict(self.party_parents)
        payables, excluded = [], []
        for b in self.bills:
            parent = parents.get(b.party_ledger)
            if parent != PAYABLE_GROUP:
                excluded.append((b, f"party parent is {parent!r}, not {PAYABLE_GROUP!r}"))
            elif not b.payable:
                excluded.append((b, f"closing balance {b.closing} is not a positive (credit) balance"))
            elif b.opening <= 0:
                excluded.append((b, f"opening balance {b.opening} is not a positive (credit) amount, so the bill "
                                    "amount cannot be stated without guessing"))
            else:
                payables.append(b)
        return tuple(payables), tuple(excluded)

    @property
    def excluded(self) -> tuple[tuple[OpenBill, str], ...]:
        return self.classify()[1]

    def platform_payload(self) -> dict[str, Any]:
        """Body for person2 ``POST /agent/open-bills`` (``OpenBillsIn``). Payables only, amounts as exact decimal
        strings with their sign unchanged (both positive for a payable); due_date and vendor_gstin never invented."""
        payables, _ = self.classify()
        return {"as_of": self.as_of.isoformat(), "bills": [
            {"party_ledger": b.party_ledger, "vendor_gstin": None, "bill_ref": b.bill_ref,
             "bill_date": b.bill_date.isoformat() if b.bill_date else None, "due_date": None,
             "amount": str(b.opening), "pending_amount": str(b.closing)}
            for b in payables]}


def bill_request(company: TallyCompany) -> bytes:
    if not isinstance(company, TallyCompany):
        raise TypeError("pin only a TallyCompany resolved from a fresh company list")
    check_name(company.name, "company name", TallyMalformedResponse)
    return _BILL_REQUEST.format(company=xml_text(company.name)).encode("utf-8")


def _leaf_text(row, tag: str) -> str | None:
    """``_text``, but a field holding a child element is refused: ``_text`` reads only the text before the first child,
    so ``1<X/>180.00`` would silently become ``1``. Comments and CDATA are not elements and read whole."""
    if any(len(e) for e in row.findall(tag)):
        raise TallyMalformedResponse(f"bill row {tag} holds a child element; mixed content is refused, not truncated")
    return _text(row, tag)


def _amount(row, tag: str, ref: str) -> Decimal:
    text = (_leaf_text(row, tag) or "").strip()
    if not _AMOUNT.fullmatch(text):
        raise TallyMalformedResponse(f"bill {ref!r} {tag} {text!r} is not a plain amount")
    return Decimal(text)


def parse_bill_export(raw: bytes, company: TallyCompany, masters: LedgerMasters) -> tuple[OpenBill, ...]:
    """Every BILL row of a Bill collection Export reply, sorted by (party, reference), or an exception; never a partial
    list. ``()`` only for a well-formed reply whose one collection is empty.

    Raises ``TallyRequestRejected`` (STATUS not 1, LINEERROR), ``CompanyNotResolved`` (masters of another company, or
    a party that is not a ledger of ``masters``) or ``TallyMalformedResponse`` (everything else, see the module doc).
    """
    if masters.company_guid != company.guid:
        raise CompanyNotResolved("masters and bills were not read from the same company")
    text = _decode(raw)
    if "\x00" in text:  # UTF-16 without a BOM: expat would still read it, past the DTD and line-break checks
        raise TallyMalformedResponse("reply holds NUL characters (UTF-16 without a byte-order mark?); refused")
    _refuse_raw_breaks_in_attributes(text)
    bills: dict[tuple[str, str], OpenBill] = {}
    for row in _only_rows(_envelope_text(text), "BILL"):
        if any(len(c) for c in row.findall("NAME")):
            raise TallyMalformedResponse("bill row NAME holds a child element; mixed content is refused")
        names = ([row.get("NAME")] if row.get("NAME") is not None else []) + [c.text or "" for c in row.findall("NAME")]
        if not names or len(set(names)) != 1:
            raise TallyMalformedResponse(f"bill row NAME missing or inconsistent: {names!r}")
        ref = plain_reply_text(names[0], "bill NAME")
        party = plain_reply_text(_leaf_text(row, "PARENT") or "", f"PARENT of bill {ref!r}")
        if party not in masters.ledgers:
            raise CompanyNotResolved(f"bill {ref!r} names party {party!r}, which is not a ledger of company "
                                     f"{company.guid}; the whole reply is refused")
        raw_date = (_leaf_text(row, "BILLDATE") or "").strip()
        bill_date = None
        if raw_date:
            if not _DATE.fullmatch(raw_date):
                raise TallyMalformedResponse(f"bill {ref!r} BILLDATE {raw_date!r} is not YYYYMMDD")
            try:
                bill_date = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
            except ValueError:
                raise TallyMalformedResponse(f"bill {ref!r} BILLDATE {raw_date!r} is not a real date") from None
        key = (party, ref.lower())
        if key in bills:
            raise TallyMalformedResponse(f"bill {ref!r} of {party!r} appears twice (as {bills[key].bill_ref!r}); "
                                         "a party's bill references must be unique regardless of case")
        bills[key] = OpenBill(party, ref, bill_date, _amount(row, "OPENINGBALANCE", ref),
                              _amount(row, "CLOSINGBALANCE", ref))
    return tuple(sorted(bills.values(), key=lambda b: (b.party_ledger, b.bill_ref)))


def fetch_open_bills(company_name: str, *, expected_guid: str | None = None,
                     transport: TallyTransport | None = None) -> OpenBills:
    """Masters then bills, both pinned and identity-checked; refused unless both reads saw the same company."""
    check_name(company_name, "company name")
    transport = transport or TallyTransport()
    masters = fetch_ledger_masters(company_name, expected_guid=expected_guid, transport=transport)
    company, raw = pinned_export(transport, company_name, bill_request, expected_guid)
    if company.guid != masters.company_guid:
        raise CompanyNotResolved("the company changed between the masters read and the bills read")
    bills = parse_bill_export(raw, company, masters)
    used = {b.party_ledger for b in bills}
    return OpenBills(company, bills, datetime.now(timezone.utc),
                     tuple((name, parent) for name, parent in masters.parents if name in used))
