"""A SYNTHETIC TallyPrime for tests: company list (real reply shape), pinned Ledger / Voucher / Bill exports, and a
voucher Import that can create, reject, hang up, stall or answer nonsense. Voucher rows are built from the real
2026-10-10 voucher reply; ledger, bill and Import replies are assumed shapes. Nothing here says how real Tally behaves.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from decimal import Decimal

from test_identity_probe_kit import _FakeTally
from test_isolation_check import OTHER_CO, TEST_CO, company_list, voucher, vouchers_reply
from test_masters import BOOK, OTHER_BOOK, ledgers_reply

CREATED = (b"<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED><LASTVCHID>{n}</LASTVCHID>"
           b"<LASTMID>0</LASTMID><COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS><CANCELLED>0</CANCELLED>"
           b"</RESPONSE>")
REJECTED = (b"<RESPONSE><LINEERROR>Voucher totals do not match! Dr: 1,180.00 Dr Cr: 0.00 Cr Diff: 1,180.00 Dr"
            b"</LINEERROR><CREATED>0</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED><LASTVCHID>0</LASTVCHID>"
            b"<LASTMID>0</LASTMID><COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>1</ERRORS><CANCELLED>0</CANCELLED>"
            b"</RESPONSE>")


def bill(ref: str, party: str, opening: str, closing: str, billdate: str = "20260401") -> str:
    return (f'<BILL NAME="{ref}"><PARENT TYPE="String">{party}</PARENT><BILLDATE TYPE="Date">{billdate}</BILLDATE>'
            f'<OPENINGBALANCE TYPE="Amount">{opening}</OPENINGBALANCE>'
            f'<CLOSINGBALANCE TYPE="Amount">{closing}</CLOSINGBALANCE></BILL>\r\n')


class FakeTally:
    """``import_mode``: create | reject | hangup_after_create | hangup_before_create | slow | garbage |
    created_not_stored | created_wrong_amount."""

    def __init__(self, *, companies=(TEST_CO, OTHER_CO), import_mode="create", slow_seconds=1.0, vouchers=None,
                 ledgers=None, bills=None):
        self.companies = list(companies)
        self.import_mode = import_mode
        self.slow_seconds = slow_seconds
        self.ledgers = ledgers if ledgers is not None else {TEST_CO[1]: BOOK, OTHER_CO[1]: OTHER_BOOK}
        self.vouchers: dict[str, list[str]] = vouchers if vouchers is not None else {TEST_CO[1]: [], OTHER_CO[1]: []}
        self.bills = bills if bills is not None else {TEST_CO[1]: [], OTHER_CO[1]: []}
        self.imports: list[bytes] = []
        self.kinds: list[str] = []
        self.down = False
        self.company_reply: bytes | None = None  # overrides the company-list reply (malformed / rejected cases)
        self.server = _FakeTally(b"", responder=self)
        self.port = self.server.port

    def close(self):
        self.server.close()

    # ---- the responder ----
    def __call__(self, body: bytes, n: int) -> bytes:
        if self.down:
            raise ConnectionResetError("simulated: Tally closed")
        root = ET.fromstring(body)
        kind = root.findtext("HEADER/TALLYREQUEST")
        if kind == "Import Data":
            self.kinds.append("import")
            return self._import(body, root)
        ctype = root.findtext(".//COLLECTION/TYPE")
        self.kinds.append(ctype)
        if ctype == "Company":
            return self.company_reply if self.company_reply is not None else company_list(self.companies)
        name = root.findtext(".//SVCURRENTCOMPANY")
        guid = next((g for nm, g in self.companies if nm == name), None)
        if guid is None:  # real TallyPrime 7.1 crashed (c0000005) on a pin to an unknown company
            raise ConnectionResetError("simulated crash: pinned to an unknown company")
        if ctype == "Ledger":
            return ledgers_reply(self.ledgers.get(guid, []))
        if ctype == "Voucher":
            return vouchers_reply(self.vouchers.get(guid, []))
        if ctype == "Bill":
            return ledgers_reply_like(self.bills.get(guid, []))
        raise AssertionError(f"unexpected request type {ctype!r}")

    def _import(self, body: bytes, root: ET.Element) -> bytes:
        self.imports.append(body)
        mode = self.import_mode
        if mode == "hangup_before_create":
            raise ConnectionResetError("simulated: connection lost before Tally saved anything")
        if mode == "slow":
            time.sleep(self.slow_seconds)
            return CREATED.replace(b"{n}", b"1")
        if mode == "garbage":
            return b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"
        if mode == "reject":
            return REJECTED
        name = root.findtext(".//SVCURRENTCOMPANY")
        guid = next(g for nm, g in self.companies if nm == name)
        if mode != "created_not_stored":
            v = root.find(".//VOUCHER")
            party = v.find("ALLLEDGERENTRIES.LIST")
            amount = Decimal(party.findtext("AMOUNT"))
            if mode == "created_wrong_amount":
                amount += 1
            self.add_voucher(guid, narration=v.findtext("NARRATION"), reference=v.findtext("REFERENCE"),
                             amount=f"{amount:.2f}")
        if mode == "hangup_after_create":
            raise ConnectionResetError("simulated: Tally saved the voucher, then the connection dropped")
        return CREATED.replace(b"{n}", str(len(self.vouchers[guid])).encode())

    def add_voucher(self, guid: str, *, narration: str, reference: str = "TEST-INV-001", amount: str = "1180.00",
                    master_id: int | None = None) -> str:
        master_id = master_id or len(self.vouchers.setdefault(guid, [])) + 1
        block = voucher(guid, master_id, doc=None)
        block = re.sub(r"<NARRATION TYPE=\"String\">.*?</NARRATION>",
                       f'<NARRATION TYPE="String">{_esc(narration)}</NARRATION>', block, flags=re.S)
        block = block.replace("TEST-INV-001", _esc(reference)).replace("1180.00", amount.lstrip("-"))
        self.vouchers[guid].append(block)
        return f"{guid}-{master_id:08x}"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ledgers_reply_like(rows: list[str]) -> bytes:
    """The same envelope as the ledger reply, holding BILL rows."""
    return ledgers_reply(rows)
