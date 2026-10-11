"""Open bills hardening (mission Part 12). NO real Bill reply has been captured, so every BILL row here is SYNTHETIC
(the assumed shape in bills.py). To stay as close to real bytes as possible, the envelope around those rows is the REAL
ledger reply (tests/fixtures/tallyprime_7_1_edu_masters_20261011_013427, with its DESC/CMPINFO counters), and the
masters are parsed from that real ledger reply. These tests prove the read's and the parser's own behaviour: identity,
refusals, exact amounts, the payables rule and the P2 payload. They say nothing about how TallyPrime answers."""

from __future__ import annotations

import ast
import inspect
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from fake_tally import FakeTally, bill
from finledger_tally import (
    AgentSettings,
    CompanyNotResolved,
    LedgerMasters,
    OpenBills,
    TallyCompany,
    TallyConnector,
    TallyMalformedResponse,
    TallyRequestRejected,
    TallyTransport,
    TallyUnavailable,
    fetch_open_bills,
    parse_bill_export,
)
from finledger_tally.bills import PAYABLE_GROUP, bill_request
from finledger_tally.company import assert_pinned_to, xml_text
from finledger_tally.masters import parse_ledger_export
from finledger_tally.transport import ReplyTooLarge, assert_export
from test_identity_probe_kit import KIT
from test_isolation_check import OTHER_CO, REJECTED, TEST_CO, company_list
from test_masters import BOOK, ledger, ledgers_reply

HERE = Path(__file__).parent
REAL_MASTERS_DIR = HERE / "fixtures" / "tallyprime_7_1_edu_masters_20261011_013427"
P2_SRC = HERE.parents[1] / "person2_platform" / "src"
P2_API = P2_SRC / "finledger_platform" / "api.py"
COMPANY = TallyCompany(*TEST_CO)
VENDOR = "FinLedger Test Vendor"

_REAL = (REAL_MASTERS_DIR / "response_02_ledgers.xml").read_bytes().decode("utf-8")  # CRLF kept as captured
_OPEN = _REAL.index(">", _REAL.index("<COLLECTION")) + 1  # just after the real COLLECTION start tag
_CLOSE = _REAL.index("   </COLLECTION>")
REAL_BOOK = parse_ledger_export((REAL_MASTERS_DIR / "response_02_ledgers.xml").read_bytes(), COMPANY)
NUMERIC_14_2 = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,2})?")


def reply(rows: list[str], *, desc_extra: str = "") -> bytes:
    """The real ledger reply's envelope (STATUS, DESC/CMPINFO, DATA, COLLECTION) holding synthetic BILL rows."""
    head = _REAL[:_OPEN].replace("</CMPINFO>", desc_extra + "</CMPINFO>")
    return (head + "\r\n" + "".join(rows) + _REAL[_CLOSE:]).encode("utf-8")


def masters(*extra: tuple[str, str]) -> LedgerMasters:
    """The real company's ledgers plus extra (ledger, parent) pairs of the same company."""
    parents = dict(REAL_BOOK.parents) | dict(extra)
    return LedgerMasters(TEST_CO[1], frozenset(parents), tuple(sorted(parents.items())), TEST_CO[0])


def parse(rows: list[str], book: LedgerMasters = REAL_BOOK):
    return parse_bill_export(reply(rows), COMPANY, book)


def open_bills(rows: list[str], book: LedgerMasters = REAL_BOOK) -> OpenBills:
    return OpenBills(COMPANY, parse(rows, book), datetime(2026, 10, 11, tzinfo=timezone.utc), book.parents)


def drop(row: str, tag: str) -> str:
    return re.sub(rf"<{tag}\b[^>]*>[^<]*</{tag}>", "", row)


def twice(row: str, tag: str) -> str:
    element = re.search(rf"<{tag}\b[^>]*>[^<]*</{tag}>", row).group(0)
    return row.replace(element, element + element)


class BillTally(FakeTally):
    """FakeTally whose Bill export can answer ``bill_reply`` (bytes, an exception to raise, or a callable), and whose
    company list shows TEST_CO's name with OTHER_CO's GUID on the calls in ``swap_on`` (1 and 2: before and after the
    masters read; 3 and 4: before and after the bills read)."""

    def __init__(self, *, bill_reply=None, swap_on=(), **kw):
        super().__init__(**kw)
        self.bill_reply, self.swap_on, self.company_calls = bill_reply, set(swap_on), 0

    def __call__(self, body: bytes, n: int) -> bytes:
        if b"<TYPE>Company</TYPE>" in body:
            self.company_calls += 1
            swapped = self.company_calls in self.swap_on
            self.company_reply = company_list([(TEST_CO[0], OTHER_CO[1])]) if swapped else None
        if b"<TYPE>Bill</TYPE>" in body and self.bill_reply is not None:
            self.kinds.append("Bill")
            if isinstance(self.bill_reply, BaseException):
                raise self.bill_reply
            return self.bill_reply() if callable(self.bill_reply) else self.bill_reply
        return super().__call__(body, n)


@pytest.fixture
def make():
    fakes = []

    def start(**kw) -> BillTally:
        fake = BillTally(**kw)
        fakes.append(fake)
        return fake

    yield start
    for fake in fakes:
        fake.close()


def connector(fake: FakeTally, *, guid: str | None = TEST_CO[1], **kw) -> TallyConnector:
    fields = dict(company_name=TEST_CO[0], company_guid=guid, port=fake.port, timeout=3)
    fields.update(kw)
    return TallyConnector(AgentSettings(**fields))


# ---------- request envelope ----------


def test_bill_request_is_one_read_only_export_with_inline_tdl_that_modifies_nothing():
    body = bill_request(COMPANY)
    root = ET.fromstring(body)
    assert {e.tag for e in root.iter()} == {"ENVELOPE", "HEADER", "VERSION", "TALLYREQUEST", "TYPE", "ID", "BODY",
                                            "DESC", "STATICVARIABLES", "SVEXPORTFORMAT", "SVCURRENTCOMPANY", "TDL",
                                            "TDLMESSAGE", "COLLECTION", "FETCH"}
    assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]
    assert (root.findtext("HEADER/TYPE"), root.findtext("HEADER/ID")) == ("Collection", "FinLedgerOpenBills")
    collections = root.findall("BODY/DESC/TDL/TDLMESSAGE/COLLECTION")
    assert len(collections) == 1 and collections[0].attrib == {"NAME": "FinLedgerOpenBills", "ISMODIFY": "No"}
    assert collections[0].findtext("TYPE") == "Bill"
    assert [f.strip() for f in collections[0].findtext("FETCH").split(",")] == [
        "NAME", "PARENT", "BILLDATE", "OPENINGBALANCE", "CLOSINGBALANCE"]
    assert root.findtext("BODY/DESC/STATICVARIABLES/SVEXPORTFORMAT") == "$$SysName:XML"
    assert not re.search(rb"SVFROMDATE|SVTODATE", body)  # no date filter is sent or relied on
    assert_export(body)
    assert assert_pinned_to(body, COMPANY) is body


@pytest.mark.parametrize("name", ["FinLedger Test Company", "R&D <Ltd> \"Main\" 'Unit'", "Imports & Exports Voucher Co",
                                  "Café ₹ Traders", "A]]>B"])
def test_bill_request_pins_any_company_name_exactly_and_escaped(name):
    body = bill_request(TallyCompany(name, TEST_CO[1]))
    assert ET.fromstring(body).findtext("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY") == name
    assert body == (KIT / "bill_export_request.xml").read_bytes().replace(b"__COMPANY__", xml_text(name).encode())
    assert_export(body)  # a name that merely contains "Import" or "Voucher" is text, not a write element


@pytest.mark.parametrize("company,error", [
    ("FinLedger Test Company", TypeError),  # a bare name was never resolved from a company list
    (TallyCompany(" FinLedger Test Company", TEST_CO[1]), TallyMalformedResponse),
    (TallyCompany("FinLedger Test Company ", TEST_CO[1]), TallyMalformedResponse),
    (TallyCompany("FinLedger\tTest Company", TEST_CO[1]), TallyMalformedResponse),
    (TallyCompany("", TEST_CO[1]), TallyMalformedResponse),
])
def test_bill_request_refuses_anything_but_a_clean_resolved_company(company, error):
    with pytest.raises(error):
        bill_request(company)


def test_kit_bill_template_passes_the_python_and_the_powershell_export_guards():
    text = (KIT / "bill_export_request.xml").read_text(encoding="utf-8").replace("__COMPANY__", TEST_CO[0])
    assert_export(text.encode("utf-8"))
    # capture.ps1 refuses with PowerShell's case-insensitive -match on exactly these patterns
    assert not re.search(r"Import|ACTION\s*=|<VOUCHER\b|<TALLYMESSAGE\b", text, re.IGNORECASE)
    assert len(re.findall(r"<TALLYREQUEST>Export</TALLYREQUEST>", text)) == 1
    assert not re.search(r"__[A-Z]+__", text)


# ---------- company identity and pinning ----------


def test_a_company_that_is_not_loaded_is_never_pinned(make):
    fake = make(companies=[OTHER_CO])
    with pytest.raises(CompanyNotResolved):
        connector(fake).fetch_open_bills()
    assert fake.kinds == ["Company"] and fake.imports == []


@pytest.mark.parametrize("guid", [TEST_CO[1], None])
def test_a_company_name_that_is_ambiguous_ignoring_case_is_never_pinned(make, guid):
    fake = make(companies=[TEST_CO, (TEST_CO[0].upper(), OTHER_CO[1])])
    with pytest.raises(CompanyNotResolved):
        connector(fake, guid=guid).fetch_open_bills()
    assert fake.kinds == ["Company"] and fake.imports == []


def test_every_pinned_read_names_exactly_the_resolved_company_and_lists_companies_around_it(make):
    fake = make()
    fake.bills[TEST_CO[1]] = [bill("INV-1", VENDOR, "1180.00", "1180.00")]
    result = connector(fake).fetch_open_bills()
    assert result.company == COMPANY and [b.bill_ref for b in result.bills] == ["INV-1"]
    assert fake.kinds == ["Company", "Ledger", "Company", "Company", "Bill", "Company"]
    pinned = [ET.fromstring(body) for body in fake.server.received if b"<TYPE>Company</TYPE>" not in body]
    assert [r.findtext("BODY/DESC/STATICVARIABLES/SVCURRENTCOMPANY") for r in pinned] == [TEST_CO[0]] * 2
    assert all(r.findtext("HEADER/TALLYREQUEST") == "Export" for r in pinned) and fake.imports == []


@pytest.mark.parametrize("guid", [TEST_CO[1], None], ids=["paired", "unpaired"])
@pytest.mark.parametrize("swap_on", [{1}, {2}, {3}, {4}, {3, 4}, {1, 2}],
                         ids=["masters-before", "masters-after", "bills-before", "bills-after", "bills-both",
                              "masters-both"])
def test_a_company_change_at_any_point_of_the_two_reads_refuses_everything(make, swap_on, guid):
    fake = make(swap_on=swap_on)
    fake.bills[TEST_CO[1]] = [bill("INV-1", VENDOR, "1180.00", "1180.00")]
    with pytest.raises(CompanyNotResolved):
        connector(fake, guid=guid).fetch_open_bills()
    assert fake.imports == []
    if guid is not None and min(swap_on) == 1:
        assert "Ledger" not in fake.kinds  # a paired GUID mismatch stops before the pinned export
    if guid is not None and min(swap_on) == 3:
        assert "Bill" not in fake.kinds


def test_without_a_paired_guid_the_bills_read_must_still_see_the_masters_company(make):
    fake = make(swap_on={3, 4})  # the masters read saw TEST_CO; the bills read sees another GUID under its name
    fake.bills[TEST_CO[1]] = [bill("INV-1", VENDOR, "1180.00", "1180.00")]
    with pytest.raises(CompanyNotResolved, match="changed between the masters read and the bills read"):
        connector(fake, guid=None).fetch_open_bills()


@pytest.mark.parametrize("name,guid,error", [
    (" FinLedger Test Company", None, ValueError),
    ("", None, ValueError),
    (None, None, ValueError),
    ("FinLedger\nTest Company", None, ValueError),
    (TEST_CO[0], "not-a-guid", ValueError),
    (TEST_CO[0], TEST_CO[1].upper(), ValueError),
])
def test_bad_arguments_are_refused_before_any_request(make, name, guid, error):
    fake = make()
    with pytest.raises(error):
        fetch_open_bills(name, expected_guid=guid, transport=TallyTransport(port=fake.port, timeout=3))
    assert fake.kinds == []


def test_a_connector_without_a_company_name_sends_nothing(make):
    fake = make()
    with pytest.raises(CompanyNotResolved):
        TallyConnector(AgentSettings(port=fake.port, timeout=3)).fetch_open_bills()
    assert fake.kinds == []


# ---------- response envelope and collection structure ----------


_ONE = bill("INV-1", VENDOR, "1180.00", "1180.00")
_FINE = reply([_ONE])


@pytest.mark.parametrize("raw,error", [
    (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", TallyMalformedResponse),
    (_FINE.replace(b"<STATUS>1</STATUS>", b"<STATUS>0</STATUS>"), TallyRequestRejected),
    (_FINE.replace(b"<STATUS>1</STATUS>", b""), TallyRequestRejected),
    (_FINE.replace(b"<DATA>", b"<DATA><LINEERROR>Could not find Report</LINEERROR>"), TallyRequestRejected),
    (REJECTED, TallyRequestRejected),
    (re.sub(rb"<COLLECTION.*</COLLECTION>", b"", _FINE, flags=re.S), TallyMalformedResponse),
    (_FINE.replace(b"</DATA>", b"<COLLECTION></COLLECTION></DATA>"), TallyMalformedResponse),
    (reply([_ONE.replace("BILL", "LEDGER")]), TallyMalformedResponse),
    (reply([_ONE, "<VOUCHER/>"]), TallyMalformedResponse),
    (reply([_ONE.replace("</BILL>", bill("INV-2", VENDOR, "5.00", "5.00") + "</BILL>")]), TallyMalformedResponse),
    (_FINE.replace(b"</DATA>", bill("INV-2", VENDOR, "5.00", "5.00").encode() + b"</DATA>"), TallyMalformedResponse),
    (_FINE.replace(b"<BODY>", b"<BODY>" + bill("INV-2", VENDOR, "5.00", "5.00").encode()), TallyMalformedResponse),
    (_FINE[:len(_FINE) // 2], TallyMalformedResponse),
    (b"", TallyMalformedResponse),
    (b'<!DOCTYPE ENVELOPE [<!ENTITY x "1180.00">]>' + _FINE, TallyMalformedResponse),
    (_FINE.replace(VENDOR.encode(), "Café".encode("latin-1")), TallyMalformedResponse),
    (_FINE.decode().encode("utf-16-le"), TallyMalformedResponse),  # UTF-16 without a byte-order mark
    (('<!DOCTYPE ENVELOPE [<!ENTITY r "INV-9">]>' + _FINE.decode()).encode("utf-16-le"), TallyMalformedResponse),
    (reply([bill("INV\n1", VENDOR, "1.00", "1.00")]).decode().encode("utf-16-le"), TallyMalformedResponse),
], ids=["not-envelope", "status-0", "status-missing", "lineerror", "company-pin-error", "no-collection",
        "two-collections", "ledger-rows", "voucher-row", "bill-nested-in-a-row", "bill-beside-the-collection",
        "bill-outside-data", "truncated", "empty-bytes", "dtd", "not-utf8", "utf16-no-bom", "utf16-no-bom-dtd",
        "utf16-no-bom-raw-lf"])
def test_replies_that_are_not_one_clean_bill_collection_are_refused_whole(raw, error):
    with pytest.raises(error):
        parse_bill_export(raw, COMPANY, REAL_BOOK)


@pytest.mark.parametrize("ref", ["INV\n1", "INV\r1", "INV\t1", "INV\r\n1"], ids=["lf", "cr", "tab", "crlf"])
def test_a_raw_line_break_in_the_reference_attribute_is_refused_not_turned_into_a_space(ref):
    with pytest.raises(TallyMalformedResponse, match="attribute value"):
        parse([bill(ref, VENDOR, "1.00", "1.00")])


def test_desc_counters_named_bill_are_not_rows():
    bills = parse_bill_export(reply([_ONE], desc_extra="<BILL>7</BILL>"), COMPANY, REAL_BOOK)
    assert [b.bill_ref for b in bills] == ["INV-1"]


def test_a_full_bill_row_inside_desc_is_refused_not_skipped():
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_bill_export(reply([_ONE], desc_extra=bill("INV-2", VENDOR, "5.00", "5.00")), COMPANY, REAL_BOOK)


def test_a_well_formed_empty_collection_is_an_empty_result_not_an_error():
    bills = open_bills([])
    assert bills.bills == () and bills.classify() == ((), ()) and bills.platform_payload()["bills"] == []


# ---------- party identity ----------


@pytest.mark.parametrize("party", [
    "Other Co Vendor",            # a ledger of another loaded company
    "finledger test vendor",      # case variant of the real ledger
    "FinLedger  Test Vendor",     # spacing variant
    "ＦinLedger Test Vendor",  # fullwidth F (same under NFKC, not the same name)
    "FTV",                        # an alias: never matched
    "Sundry Creditors",           # a group, not a ledger
])
def test_a_party_that_is_not_an_exact_ledger_of_the_company_refuses_the_whole_reply(party):
    book = LedgerMasters(TEST_CO[1], REAL_BOOK.ledgers, REAL_BOOK.parents, TEST_CO[0],
                         aliases=(("FTV", VENDOR),))
    with pytest.raises(CompanyNotResolved, match="not a ledger of company"):
        parse([_ONE, bill("X-1", party, "1.00", "1.00")], book)


@pytest.mark.parametrize("row", [
    bill("X", "", "1.00", "1.00"),
    bill("X", "   ", "1.00", "1.00"),
    bill("X", f" {VENDOR}", "1.00", "1.00"),
    bill("X", f"{VENDOR}&#10;", "1.00", "1.00"),
    drop(bill("X", VENDOR, "1.00", "1.00"), "PARENT"),
    twice(bill("X", VENDOR, "1.00", "1.00"), "PARENT"),
], ids=["empty", "blank", "padded", "control-char", "missing", "repeated"])
def test_a_missing_blank_padded_or_repeated_party_is_refused(row):
    with pytest.raises(TallyMalformedResponse):
        parse([row])


def test_bills_naming_another_companys_vendor_are_refused_end_to_end(make):
    fake = make()
    fake.bills[TEST_CO[1]] = [_ONE, bill("X-1", "Other Co Vendor", "1.00", "1.00")]
    with pytest.raises(CompanyNotResolved):
        connector(fake).fetch_open_bills()


# ---------- bill reference ----------


@pytest.mark.parametrize("row", [
    bill("", VENDOR, "1.00", "1.00"),
    bill("   ", VENDOR, "1.00", "1.00"),
    bill(" INV-1", VENDOR, "1.00", "1.00"),
    bill("INV-1 ", VENDOR, "1.00", "1.00"),
    bill("INV&#10;1", VENDOR, "1.00", "1.00"),
    bill("INV&#9;1", VENDOR, "1.00", "1.00"),
    bill("INV-1", VENDOR, "1.00", "1.00").replace(' NAME="INV-1"', ""),
    bill("INV-1", VENDOR, "1.00", "1.00").replace("<PARENT", "<NAME>INV-2</NAME><PARENT"),
], ids=["empty", "blank", "leading-space", "trailing-space", "escaped-lf", "escaped-tab", "missing",
        "attribute-and-child-disagree"])
def test_a_bill_reference_must_be_present_and_plain(row):
    with pytest.raises(TallyMalformedResponse):
        parse([row])


@pytest.mark.parametrize("row,ref", [
    (bill("R&amp;D/1 &lt;A&gt;", VENDOR, "1.00", "1.00"), "R&D/1 <A>"),
    (bill("X", VENDOR, "1.00", "1.00").replace(' NAME="X"', "").replace("<PARENT", "<NAME>INV/7</NAME><PARENT"),
     "INV/7"),
    (bill("INV-9", VENDOR, "1.00", "1.00").replace("<PARENT", "<NAME>INV-9</NAME><PARENT"), "INV-9"),
    (bill("बिल-१ ₹", VENDOR, "1.00", "1.00"), "बिल-१ ₹"),
    (bill("INV  2", VENDOR, "1.00", "1.00"), "INV  2"),
], ids=["entities", "name-child-only", "attribute-equals-child", "non-ascii", "inner-spaces-kept"])
def test_a_bill_reference_is_read_exactly(row, ref):
    assert [b.bill_ref for b in parse([row])] == [ref]


@pytest.mark.parametrize("first,second", [("INV-1", "INV-1"), ("INV-1", "inv-1"), ("Inv-1", "INV-1")])
def test_duplicate_references_for_one_party_refuse_the_reply(first, second):
    with pytest.raises(TallyMalformedResponse, match="appears twice"):
        parse([bill(first, VENDOR, "1.00", "1.00"), bill(second, VENDOR, "2.00", "2.00")])


@pytest.mark.parametrize("first,second", [("MASS-1", "Maß-1"), ("INV-ﬁ", "INV-FI"), ("INV-1", "INV-Ⅰ")])
def test_references_that_differ_beyond_case_are_two_bills_of_one_party(first, second):
    """The duplicate rule is case only (``str.lower``), not full Unicode folding, so one unusual reference cannot block
    the whole read; both bills are kept exactly as read."""
    bills = parse([bill(first, VENDOR, "1.00", "1.00"), bill(second, VENDOR, "2.00", "2.00")])
    assert sorted(b.bill_ref for b in bills) == sorted([first, second])


def test_the_same_reference_under_two_parties_is_two_bills():
    book = masters(("Second Vendor", PAYABLE_GROUP))
    result = open_bills([bill("INV-1", VENDOR, "1180.00", "1180.00"), bill("INV-1", "Second Vendor", "50.00", "50.00")],
                        book)
    assert [(b.party_ledger, b.bill_ref, b.closing) for b in result.bills] == [
        (VENDOR, "INV-1", Decimal("1180.00")), ("Second Vendor", "INV-1", Decimal("50.00"))]
    assert [(r["party_ledger"], r["pending_amount"]) for r in result.platform_payload()["bills"]] == [
        (VENDOR, "1180.00"), ("Second Vendor", "50.00")]


# ---------- mixed content: a field holding a child element is refused, never truncated to its leading text ----------

_ROW = bill("INV-1", VENDOR, "1180.00", "1180.00")
_NAME_CHILD_ROW = ('<BILL><NAME>INV-1</NAME><PARENT>{p}</PARENT><BILLDATE>20260401</BILLDATE>'
                   '<OPENINGBALANCE>1180.00</OPENINGBALANCE><CLOSINGBALANCE>1180.00</CLOSINGBALANCE></BILL>\r\n')


@pytest.mark.parametrize("row", [
    _ROW.replace(">1180.00</CLOSINGBALANCE>", ">1<X/>180.00</CLOSINGBALANCE>"),
    _ROW.replace(">1180.00</OPENINGBALANCE>", ">1<X/>180.00</OPENINGBALANCE>"),
    _ROW.replace(">1180.00</CLOSINGBALANCE>", "><X>1180.00</X></CLOSINGBALANCE>"),
    _ROW.replace(">1180.00</OPENINGBALANCE>", ">1180.00<X/></OPENINGBALANCE>"),
    _ROW.replace(">20260401</BILLDATE>", ">2026<X/>0401</BILLDATE>"),
    _ROW.replace(">20260401</BILLDATE>", "><X/></BILLDATE>"),
    _ROW.replace(f">{VENDOR}</PARENT>", f">{VENDOR}<X/> Other</PARENT>"),
    _NAME_CHILD_ROW.format(p=VENDOR).replace("<NAME>INV-1</NAME>", "<NAME>INV<X/>-1</NAME>"),
    _NAME_CHILD_ROW.format(p=VENDOR).replace("<NAME>INV-1</NAME>", "<NAME>INV-1<X/></NAME>"),
    _ROW.replace("<PARENT", "<NAME>INV-1<X/></NAME><PARENT"),
], ids=["closing-split", "opening-split", "closing-wrapped", "opening-trailing", "date-split", "date-only-child",
        "parent-split", "name-child-split", "name-child-trailing", "name-child-beside-attribute"])
def test_a_field_holding_a_child_element_is_refused_not_truncated(row):
    with pytest.raises(TallyMalformedResponse, match="child element"):
        parse([row])


def test_comments_and_cdata_inside_an_amount_are_read_whole():
    row = _ROW.replace(">1180.00</CLOSINGBALANCE>", ">1<!-- c -->180.00</CLOSINGBALANCE>").replace(
        ">1180.00</OPENINGBALANCE>", "><![CDATA[1180.00]]></OPENINGBALANCE>")
    (b,) = parse([row])
    assert (b.opening, b.closing) == (Decimal("1180.00"), Decimal("1180.00"))


# ---------- outstanding amounts and sign ----------


@pytest.mark.parametrize("text", ["0.01", "1", "1.5", "1180.00", "0", "-0.00", "-500.00", "999999999999.99",
                                  "-999999999999.99", "007.10", " 1180.00 "])
def test_amounts_are_read_exactly_and_never_rounded_or_normalised(text):
    (b,) = parse([bill("X", VENDOR, text, text)])
    exact = Decimal(text.strip())
    assert b.opening.as_tuple() == b.closing.as_tuple() == exact.as_tuple()  # same digits and places: not quantized
    assert open_bills([bill("X", VENDOR, "1.00", text)]).bills[0].closing.as_tuple() == exact.as_tuple()


@pytest.mark.parametrize("text", ["1.005", "1.999", "0.001", "1e3", "1E3", "NaN", "-NaN", "sNaN", "Infinity",
                                  "-Infinity", "+1.00", "1180.00 Cr", "1180.00 Dr", "1,180.00", "", "   ",
                                  "1234567890123.00", ".5", "5.", "--1", "1_000", "0x10", "₹1180",
                                  "١٢٣.٠٠", "１２３", "1 180"])
@pytest.mark.parametrize("field", ["OPENINGBALANCE", "CLOSINGBALANCE"])
def test_amounts_that_are_not_plain_ascii_decimals_with_two_places_at_most_are_refused(text, field):
    row = bill("X", VENDOR, "1.00", "1.00")
    row = row.replace(f'<{field} TYPE="Amount">1.00</{field}>', f'<{field} TYPE="Amount">{text}</{field}>')
    with pytest.raises(TallyMalformedResponse, match="not a plain amount"):
        parse([row])


@pytest.mark.parametrize("field", ["PARENT", "BILLDATE", "OPENINGBALANCE", "CLOSINGBALANCE"])
def test_a_repeated_field_is_refused(field):
    with pytest.raises(TallyMalformedResponse, match="appears 2 times"):
        parse([twice(bill("X", VENDOR, "1.00", "1.00"), field)])


@pytest.mark.parametrize("field", ["OPENINGBALANCE", "CLOSINGBALANCE"])
def test_a_missing_amount_is_refused_never_read_as_zero(field):
    with pytest.raises(TallyMalformedResponse, match="not a plain amount"):
        parse([drop(bill("X", VENDOR, "1.00", "1.00"), field)])


@pytest.mark.parametrize("opening", ["-100.00", "0", "0.00", "-0.00"])
def test_a_credit_bill_whose_opening_is_not_a_credit_is_excluded_not_sign_flipped(opening):
    result = open_bills([bill("FLIP", VENDOR, opening, "50.00")])
    (b,) = result.bills
    assert b.payable and b.opening == Decimal(opening)  # read as given
    assert result.classify()[0] == () and "opening balance" in result.classify()[1][0][1]
    assert result.platform_payload()["bills"] == []


def test_the_payload_keeps_amounts_exactly_as_read_with_positive_meaning_credit():
    result = open_bills([bill("INV-1", VENDOR, "1180.00", "1180.00"), bill("PART", VENDOR, "1000.00", "250.50"),
                         bill("TINY", VENDOR, "0.10", "0.20")])
    assert [(r["bill_ref"], r["amount"], r["pending_amount"]) for r in result.platform_payload()["bills"]] == [
        ("INV-1", "1180.00", "1180.00"), ("PART", "1000.00", "250.50"), ("TINY", "0.10", "0.20")]


# ---------- payables rule: nothing dropped, every exclusion has a reason ----------


GRID_BOOK = masters(("Second Vendor", PAYABLE_GROUP), ("Debtor One", "Sundry Debtors"),
                    ("Local Vendor", "Local Creditors"), ("Lowercase Vendor", "sundry creditors"))
GRID = [(party, opening, closing)
        for party in (VENDOR, "Second Vendor", "Debtor One", "Local Vendor", "Lowercase Vendor", "Cash")
        for opening in ("1180.00", "0", "-50.00")
        for closing in ("1180.00", "0.01", "0.00", "-500.00")]


def test_every_bill_read_is_either_a_payable_or_excluded_with_a_reason():
    rows = [bill(f"R{i:02d}", party, opening, closing) for i, (party, opening, closing) in enumerate(GRID)]
    result = open_bills(rows, GRID_BOOK)
    payables, excluded = result.classify()
    assert len(result.bills) == len(rows)  # every BILL row became one bill: nothing merged or dropped
    assert len(payables) + len(excluded) == len(result.bills)
    assert {b.bill_ref for b in payables}.isdisjoint({b.bill_ref for b, _ in excluded})
    assert {b.bill_ref for b in payables} | {b.bill_ref for b, _ in excluded} == {b.bill_ref for b in result.bills}
    assert all(isinstance(why, str) and why for _, why in excluded)
    expected = {f"R{i:02d}" for i, (party, opening, closing) in enumerate(GRID)
                if GRID_BOOK.parent_of(party) == PAYABLE_GROUP and Decimal(opening) > 0 and Decimal(closing) > 0}
    assert {b.bill_ref for b in payables} == expected and len(expected) == 4
    assert [r["bill_ref"] for r in result.platform_payload()["bills"]] == sorted(expected)
    assert result.excluded == excluded


@pytest.mark.parametrize("party,parent", [("Debtor One", "Sundry Debtors"), ("Local Vendor", "Local Creditors"),
                                          ("Lowercase Vendor", "sundry creditors"), ("Cash", "Cash-in-Hand")])
def test_only_parties_directly_under_sundry_creditors_are_payables(party, parent):
    result = open_bills([bill("X", party, "100.00", "100.00")], GRID_BOOK)
    ((b, why),) = result.classify()[1]
    assert b.party_ledger == party and f"party parent is {parent!r}" in why and result.classify()[0] == ()


# ---------- dates ----------


@pytest.mark.parametrize("text", ["20260231", "20250229", "20261301", "20260001", "20260100", "00000000",
                                  "2026041", "202604011", "2026-04-01", "01-04-2026", "1-Apr-26", "2026O401",
                                  "２０２６０４０１",
                                  "٢٠٢٦٠٤٠١"])
def test_bill_dates_must_be_real_ascii_yyyymmdd(text):
    with pytest.raises(TallyMalformedResponse, match="BILLDATE"):
        parse([bill("X", VENDOR, "1.00", "1.00", billdate=text)])


@pytest.mark.parametrize("text,day", [("20260401", date(2026, 4, 1)), ("20240229", date(2024, 2, 29)),
                                      ("19000101", date(1900, 1, 1)), ("20991231", date(2099, 12, 31)),
                                      (" 20260401 ", date(2026, 4, 1))])
def test_valid_bill_dates_are_read_exactly(text, day):
    (b,) = parse([bill("X", VENDOR, "1.00", "1.00", billdate=text)])
    assert b.bill_date == day
    assert open_bills([bill("X", VENDOR, "1.00", "1.00", billdate=text)]).platform_payload()["bills"][0][
        "bill_date"] == day.isoformat()


@pytest.mark.parametrize("row", [drop(bill("X", VENDOR, "1.00", "1.00"), "BILLDATE"),
                                 bill("X", VENDOR, "1.00", "1.00", billdate=""),
                                 bill("X", VENDOR, "1.00", "1.00", billdate="   ")],
                         ids=["absent", "empty", "blank"])
def test_a_missing_bill_date_is_none_never_guessed_and_the_bill_still_counts(row):
    result = open_bills([row])
    (b,) = result.bills
    assert b.bill_date is None and result.classify()[0] == (b,)
    assert result.platform_payload()["bills"][0]["bill_date"] is None


def test_due_date_and_gstin_are_never_filled_even_when_the_reply_has_look_alike_fields():
    row = bill("X", VENDOR, "1.00", "1.00").replace(
        "</BILL>", "<DUEDATE>20260501</DUEDATE><BILLCREDITPERIOD>30 Days</BILLCREDITPERIOD>"
                   "<PARTYGSTIN>27AAAAA0000A1Z5</PARTYGSTIN></BILL>")
    (sent,) = open_bills([row]).platform_payload()["bills"]
    assert sent["due_date"] is None and sent["vendor_gstin"] is None


def test_date_filters_are_neither_sent_nor_assumed(make):
    assert list(inspect.signature(fetch_open_bills).parameters) == ["company_name", "expected_guid", "transport"]
    fake = make()
    fake.bills[TEST_CO[1]] = [bill("OLD", VENDOR, "1.00", "1.00", billdate="19000101"),
                              bill("NEW", VENDOR, "2.00", "2.00", billdate="20991231")]
    result = connector(fake).fetch_open_bills()
    assert [b.bill_ref for b in result.bills] == ["NEW", "OLD"]  # both kept: no date scoping anywhere
    bills_request = next(body for body in fake.server.received if b"<TYPE>Bill</TYPE>" in body)
    assert bills_request == bill_request(COMPANY) and b"SVFROMDATE" not in bills_request


# ---------- encoding ----------


def test_utf8_text_and_entities_round_trip_exactly():
    party = "Café ₹ Traders & Co <Pune>"
    book = masters((party, PAYABLE_GROUP))
    result = open_bills([bill("बिल/१ &amp; &quot;A&quot;", xml_text(party), "1180.00", "1180.00")], book)
    (sent,) = result.platform_payload()["bills"]
    assert (sent["party_ledger"], sent["bill_ref"]) == (party, 'बिल/१ & "A"')


@pytest.mark.parametrize("encode", [lambda t: t.encode("utf-8"), lambda t: t.encode("utf-8-sig"),
                                    lambda t: t.encode("utf-16")], ids=["utf-8", "utf-8-bom", "utf-16-bom"])
def test_byte_order_marks_do_not_change_the_bills(encode):
    rows = [bill("INV-₹1", VENDOR, "1180.00", "1180.00"), bill("ADV-1", VENDOR, "-5.00", "-5.00")]
    expected = parse(rows)
    assert parse_bill_export(encode(reply(rows).decode("utf-8")), COMPANY, REAL_BOOK) == expected


# ---------- failed request vs empty result, end to end ----------


def _slow():
    time.sleep(1.0)
    return reply([])


@pytest.mark.parametrize("bill_reply,error", [
    (ConnectionResetError("simulated: Tally closed during the bills read"), TallyUnavailable),
    (_slow, TallyUnavailable),
    (REJECTED, TallyRequestRejected),
    (reply([]).replace(b"<STATUS>1</STATUS>", b"<STATUS>0</STATUS>"), TallyRequestRejected),
    (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", TallyMalformedResponse),
    (b"", TallyMalformedResponse),
    (re.sub(rb"<COLLECTION.*</COLLECTION>", b"", reply([]), flags=re.S), TallyMalformedResponse),
], ids=["connection-lost", "timeout", "rejected", "status-0", "garbage", "empty-body", "no-collection"])
def test_a_failed_bill_read_raises_and_never_returns_an_empty_list(make, bill_reply, error):
    fake = make(bill_reply=bill_reply)
    with pytest.raises(error):
        connector(fake, timeout=0.3).fetch_open_bills()
    assert "Bill" in fake.kinds and fake.imports == []


def test_an_oversized_bill_reply_is_refused_not_truncated(make):
    bound = max(len(company_list([TEST_CO, OTHER_CO])), len(ledgers_reply(BOOK))) + 1024
    rows = [bill(f"INV-{i}", VENDOR, "1.00", "1.00") for i in range(bound // 100 + 50)]
    fake = make(bill_reply=reply(rows))
    assert len(reply(rows)) > bound
    with pytest.raises(ReplyTooLarge):
        connector(fake, max_reply_bytes=bound).fetch_open_bills()


def test_a_valid_empty_bill_reply_is_an_empty_result_after_every_identity_check(make):
    fake = make(bill_reply=reply([]))
    result = connector(fake).fetch_open_bills()
    assert result.bills == () and result.platform_payload()["bills"] == [] and result.company == COMPANY
    assert fake.kinds == ["Company", "Ledger", "Company", "Company", "Bill", "Company"]


def test_parties_of_the_payload_come_from_the_same_calls_masters(make):
    fake = make()
    fake.ledgers[TEST_CO[1]] = BOOK + [ledger("Second Vendor", PAYABLE_GROUP, n=0x1c)]
    fake.bills[TEST_CO[1]] = [bill("A", VENDOR, "1.00", "1.00"), bill("B", "Second Vendor", "2.00", "2.00"),
                              bill("C", "Cash", "3.00", "3.00")]
    result = connector(fake).fetch_open_bills()
    assert dict(result.party_parents) == {VENDOR: PAYABLE_GROUP, "Second Vendor": PAYABLE_GROUP,
                                          "Cash": "Cash-in-Hand"}
    assert [r["bill_ref"] for r in result.platform_payload()["bills"]] == ["A", "B"]


# ---------- Person 2 OpenBillsIn shape ----------


def _person2_models_from_source():
    """P2's own OpenBill / OpenBillsIn class definitions, compiled from api.py without importing its app (whose
    dependencies, fastapi and psycopg, may be absent)."""
    pydantic = pytest.importorskip("pydantic")
    tree = ast.parse(P2_API.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ("OpenBill", "OpenBillsIn")]
    assert [n.name for n in nodes] == ["OpenBill", "OpenBillsIn"], "person2 renamed or moved its open-bills models"
    namespace = {"__name__": "person2_open_bills_models", "BaseModel": pydantic.BaseModel, "Field": pydantic.Field,
                 "datetime": datetime}
    code = compile(ast.Module(body=nodes, type_ignores=[]), str(P2_API), "exec", dont_inherit=True)  # eager annotations
    exec(code, namespace)  # noqa: S102 - person2's own class bodies from this repository
    return namespace["OpenBill"], namespace["OpenBillsIn"]


def _sample_payload() -> dict:
    book = masters(("Second Vendor", PAYABLE_GROUP))
    return OpenBills(COMPANY, parse([bill("INV-1", VENDOR, "1180.00", "1180.00"),
                                     bill("MAX", "Second Vendor", "999999999999.99", "0.01", billdate=""),
                                     bill("TINY", VENDOR, "0.10", "0.20"),
                                     bill("DEBIT", VENDOR, "-5.00", "-5.00")], book),
                     datetime.now(timezone.utc), book.parents).platform_payload()


def test_platform_payload_matches_person2_open_bill_models_field_for_field():
    open_bill, open_bills_in = _person2_models_from_source()
    payload = _sample_payload()
    assert set(payload) == set(open_bills_in.model_fields)
    assert all(set(row) == set(open_bill.model_fields) for row in payload["bills"])  # nothing P2 would ignore
    for row in payload["bills"]:
        assert isinstance(row["party_ledger"], str) and isinstance(row["bill_ref"], str)
        assert NUMERIC_14_2.fullmatch(row["amount"]) and NUMERIC_14_2.fullmatch(row["pending_amount"])
        assert row["bill_date"] is None or date.fromisoformat(row["bill_date"])
        assert row["due_date"] is None and row["vendor_gstin"] is None
    parsed = open_bills_in.model_validate(payload)
    assert parsed.as_of.tzinfo is not None and len(parsed.bills) == 3
    for sent, got in zip(payload["bills"], parsed.bills):
        # P2 still declares floats (gap 5); every value we send survives that parse digit for digit
        assert Decimal(str(got.amount)) == Decimal(sent["amount"])
        assert Decimal(str(got.pending_amount)) == Decimal(sent["pending_amount"])
        assert (got.party_ledger, got.bill_ref, got.bill_date, got.due_date) == (
            sent["party_ledger"], sent["bill_ref"], sent["bill_date"], None)


def test_platform_payload_validates_against_the_importable_person2_api(monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.syspath_prepend(str(P2_SRC))
    try:
        from finledger_platform.api import OpenBillsIn  # person2's own request model
    except Exception as exc:  # noqa: BLE001 - person2 deps (psycopg etc.) may be absent here
        pytest.skip(f"person2 package not importable here: {exc}")
    OpenBillsIn.model_validate(_sample_payload())


# ---------- Run-Bills-Capture.ps1: read-only by construction (static; the pwsh run tests are in test_bills_capture) --


def test_bills_capture_script_sends_only_exports_and_writes_only_under_its_captures_folder():
    src = (KIT / "Run-Bills-Capture.ps1").read_text(encoding="ascii")
    calls = re.findall(r"& \$capture ([^\r\n]*)", src)
    assert [re.search(r"-Kind (\w+)", c).group(1) for c in calls] == ["company", "bill", "company"]
    assert all("-OutDir $runDir" in c for c in calls)
    assert "-Company $before.Company.Name" in calls[1]  # pins only the name resolved from the fresh company list
    assert "[string]$OutRoot = (Join-Path $PSScriptRoot 'captures')" in src
    assert '$runDir = Join-Path $OutRoot "bills_$stamp"' in src
    writes = re.findall(r"WriteAll\w+|Compress-Archive|New-Item|Set-Content|Out-File|Add-Content|Copy-Item|Move-Item|"
                        r"Remove-Item|Rename-Item|Clear-Content|Export-\w+|(?<![-<])>>?\s*\$", src)
    assert sorted(writes) == ["Compress-Archive", "New-Item", "New-Item", "WriteAllLines"]
    assert "New-Item -ItemType Directory -Path $OutRoot" in src and "New-Item -ItemType Directory -Path $runDir" in src
    assert "WriteAllLines((Join-Path $runDir 'bills_summary.txt')" in src
    assert "$zip = Join-Path $OutRoot" in src and "-DestinationPath $zip" in src
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"Import|TALLYMESSAGE|ACTION\s*=|<VOUCHER", code, re.IGNORECASE)
    # every request goes through capture.ps1 (Export-guarded); no second send path that need not say "Import"
    assert not re.search(r"Invoke-WebRequest|Invoke-RestMethod|\biwr\b|\birm\b|WebClient|HttpClient|WebRequest|"
                         r"UdpClient|\bcurl\b|\bwget\b|Start-BitsTransfer|GetStream|\.Send\(|NetworkStream", code,
                         re.IGNORECASE)
    # the one socket is a connect-and-close reachability probe of local Tally: it sends no bytes
    assert re.findall(r"TcpClient|Net\.Sockets", code) == ["Net.Sockets", "TcpClient"]
    assert re.findall(r"\$client\.\w+", code) == ["$client.ConnectAsync", "$client.Connected", "$client.Close"]
    assert "$client.ConnectAsync('127.0.0.1', $Port)" in code
