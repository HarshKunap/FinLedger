"""Round-2 identity hardening: no foreign company names or GUIDs in messages, stray rows, mixed content, NUL
characters, linear marker-like matching.

Every reply here is SYNTHETIC, built from the REAL 2026-10-10 capture (tests/fixtures/tallyprime_7_1_edu_run_20261010_
162316); company lists are served by an in-process stub transport (no socket at all). Nothing talks to a real Tally.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path

import pytest

from finledger_tally import (
    CompanyNotResolved,
    LookupStatus,
    TallyCompany,
    TallyMalformedResponse,
    find_by_marker,
    parse_company_list,
    parse_voucher_export,
    resolve_company,
)
from finledger_tally.company import assert_pinned_to, current_company, pinned_export
from finledger_tally.identity import _MARKER_LIKE
from finledger_tally.lookup import voucher_request
from finledger_tally.transport import RequestRefused
from test_isolation_check import OTHER_CO, TEST_CO, company_list, voucher, vouchers_reply

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
COMPANIES = (REAL / "response_01_companies.xml").read_bytes()
VOUCHERS = (REAL / "response_02_day_before_edit.xml").read_bytes()
DAY = date(2026, 4, 1)
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
CLIENT_B = ("Client B Pvt Ltd", "1b1c2d3e-4f50-4a61-8b72-c83d94e5f601")  # synthetic: another CA client
CLIENT_C = ("Client C LLP", "2c1c2d3e-4f50-4a61-8b72-c83d94e5f602")      # synthetic: another CA client
FOREIGN = [OTHER_CO, CLIENT_B, CLIENT_C]


def _no_foreign(text: str) -> None:
    for name, guid in FOREIGN:
        assert name not in text and guid not in text, text


class _ListOnly:
    """Stub transport: every Export is answered with the next queued company-list reply. No network at all."""

    def __init__(self, *replies: bytes):
        self.replies = list(replies)
        self.sent: list[bytes] = []

    def export(self, request: bytes) -> bytes:
        self.sent.append(request)
        return self.replies.pop(0)


# ---------- 1. messages count companies; they never name another company ----------


@pytest.mark.parametrize("loaded,name", [
    (FOREIGN, TEST_CO[0]),                                      # not loaded
    ([], TEST_CO[0]),                                           # nothing loaded
    (FOREIGN + [TEST_CO, (TEST_CO[0], CLIENT_B[1])], TEST_CO[0]),  # exact twin with a foreign GUID
    (FOREIGN + [TEST_CO, (TEST_CO[0].upper(), CLIENT_C[1])], TEST_CO[0]),  # case twin
], ids=["not-loaded", "none-loaded", "exact-twin", "case-twin"])
def test_resolve_company_message_holds_counts_and_the_configured_name_only(loaded, name):
    companies = [TallyCompany(n, g) for n, g in loaded]
    with pytest.raises(CompanyNotResolved) as err:
        resolve_company(companies, name)
    _no_foreign(str(err.value))
    assert repr(name) in str(err.value) and f"({len(companies)} loaded in all)" in str(err.value)
    assert TEST_CO[1] not in str(err.value)  # not even the configured company's GUID is needed to explain it


def test_resolve_company_message_for_a_non_string_name_lists_nothing():
    with pytest.raises(CompanyNotResolved) as err:
        resolve_company([TallyCompany(*c) for c in FOREIGN], None)
    _no_foreign(str(err.value))


def test_current_company_with_a_foreign_guid_names_only_the_paired_guid():
    # A different company carrying the configured name: its GUID belongs to another client and is not quoted.
    stub = _ListOnly(company_list([(TEST_CO[0], CLIENT_B[1]), OTHER_CO]))
    with pytest.raises(CompanyNotResolved, match="paired") as err:
        current_company(stub, TEST_CO[0], expected_guid=TEST_CO[1])
    _no_foreign(str(err.value))
    assert TEST_CO[1] in str(err.value)


def test_pinned_export_guid_change_message_quotes_no_guid():
    stub = _ListOnly(company_list([TEST_CO, OTHER_CO]), b"<reply/>", company_list([(TEST_CO[0], CLIENT_B[1])]))
    with pytest.raises(CompanyNotResolved, match="changed") as err:
        pinned_export(stub, TEST_CO[0], lambda c: voucher_request(c, DAY, DAY))
    _no_foreign(str(err.value))
    assert TEST_CO[1] not in str(err.value)


def test_foreign_voucher_reason_counts_rows_and_never_quotes_a_foreign_guid():
    rows = parse_voucher_export(vouchers_reply([voucher(TEST_CO[1], 1), voucher(CLIENT_B[1], 2, doc=None),
                                                voucher(CLIENT_C[1], 3, doc=None)]))
    result = find_by_marker(rows, DOC, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and "not company-scoped" in result.reason
    assert "2 of 3" in result.reason and result.row is None and result.rows == ()
    _no_foreign(result.reason)


def test_unattributable_voucher_reason_counts_rows_and_quotes_no_guid():
    odd = f"{CLIENT_B[1]}-xyz"  # foreign-looking but not <company GUID>-<8 hex>
    rows = parse_voucher_export(vouchers_reply([voucher(TEST_CO[1], 1), voucher(TEST_CO[1], 2, doc=None, guid=odd)]))
    result = find_by_marker(rows, DOC, TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and "1 of 2" in result.reason and result.rows == ()
    _no_foreign(result.reason)


# ---------- 2. stray rows anywhere outside the collection ----------


def _voucher_row() -> bytes:
    return VOUCHERS[VOUCHERS.index(b"<VOUCHER "):VOUCHERS.rindex(b"</VOUCHER>") + len(b"</VOUCHER>")]


def _company_row() -> bytes:
    return COMPANIES[COMPANIES.index(b"<COMPANY "):COMPANIES.rindex(b"</COMPANY>") + len(b"</COMPANY>")]


@pytest.mark.parametrize("edit", [
    lambda v: v.replace(b"</DESC>", _voucher_row() + b"</DESC>", 1),                 # full row under BODY/DESC
    lambda v: v.replace(b"</CMPINFO>", _voucher_row() + b"</CMPINFO>", 1),           # full row inside CMPINFO
    lambda v: v.replace(b"<CMPINFO>", b"<CMPINFO><X><VOUCHER>6</VOUCHER></X>", 1),  # counter-shaped, but nested
    lambda v: v.replace(b"</DESC>", b"<VOUCHER>6</VOUCHER></DESC>", 1),              # counter-shaped, not in CMPINFO
    lambda v: v.replace(b"</COLLECTION>", b"</COLLECTION><voucher><GUID>x</GUID></voucher>", 1),  # lower case
    lambda v: v.replace(b"</DATA>", b"</DATA><Voucher/>", 1),                          # mixed case, empty
], ids=["desc-row", "cmpinfo-row", "nested-counter", "desc-counter", "lower-after-collection", "mixed-case"])
def test_a_voucher_element_outside_the_collection_refuses_the_reply(edit):
    assert len(parse_voucher_export(VOUCHERS)) == 1  # the real fixture, CMPINFO counters included, still parses
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_voucher_export(edit(VOUCHERS))


def test_a_company_row_outside_the_collection_refuses_the_company_list():
    assert len(parse_company_list(COMPANIES)) == 1
    for bad in (COMPANIES.replace(b"</DESC>", _company_row() + b"</DESC>", 1),
                COMPANIES.replace(b"</CMPINFO>", _company_row().lower() + b"</CMPINFO>", 1)):
        with pytest.raises(TallyMalformedResponse, match="outside the collection"):
            parse_company_list(bad)


# ---------- 3. mixed content in a field is refused, never truncated ----------


@pytest.mark.parametrize("old,new", [
    (b"TEST-INV-001</REFERENCE>", b"A<X/>B</REFERENCE>"),
    (b"TEST-INV-001</REFERENCE>", b"TEST-INV-001<X/></REFERENCE>"),
    (b"Vendor</PARTYLEDGERNAME>", b"Vendor<X>y</X> Other</PARTYLEDGERNAME>"),
    (b"e98348e5b503</NARRATION>", b"e98348e5b503<B/>tail</NARRATION>"),
    (b"</GUID>", b"<X/></GUID>"),
], ids=["reference-mixed", "reference-child", "party-mixed", "narration-mixed", "guid-child"])
def test_voucher_field_with_a_child_element_is_refused(old, new):
    assert old in VOUCHERS
    with pytest.raises(TallyMalformedResponse, match="mixed content"):
        parse_voucher_export(VOUCHERS.replace(old, new, 1))


def test_company_name_with_a_child_element_is_refused():
    with pytest.raises(TallyMalformedResponse, match="mixed content"):
        parse_company_list(COMPANIES.replace(b"</NAME>", b"<X/> Client B</NAME>", 1))


def test_comments_are_not_child_elements():
    # ElementTree drops comments, so the field reads whole; the real shapes still parse.
    rows = parse_voucher_export(VOUCHERS.replace(b"TEST-INV-001</REFERENCE>", b"TEST-<!-- c -->INV-001</REFERENCE>"))
    assert rows[0].reference == "TEST-INV-001"


# ---------- 4. the pin is a plain-text leaf ----------


@pytest.mark.parametrize("pin", [
    TEST_CO[0] + "<X/> Other",
    TEST_CO[0] + "<X/>",
    "<X/>" + TEST_CO[0],
    TEST_CO[0] + "<X>" + TEST_CO[0] + "</X>",
], ids=["child-and-tail", "trailing-child", "leading-child", "nested-copy"])
def test_a_pin_with_a_child_element_is_refused(pin):
    company = TallyCompany(*TEST_CO)
    good = voucher_request(company, DAY, DAY)
    assert assert_pinned_to(good, company) is good
    bad = re.sub(rb"<SVCURRENTCOMPANY>[^<]*</SVCURRENTCOMPANY>",
                 b"<SVCURRENTCOMPANY>" + pin.encode() + b"</SVCURRENTCOMPANY>", good)
    assert bad != good
    with pytest.raises(RequestRefused):
        assert_pinned_to(bad, company)


# ---------- 5. marker-like matching is linear and matches what it matched before ----------

_OLD_MARKER_LIKE = re.compile(r"finledger\s*:?\s*document[_\s-]*id", re.IGNORECASE)


@pytest.mark.parametrize("text", [
    f"finledger:document_id={DOC}", f"FinLedger: document id {DOC}", "finledger document-id", "finledgerdocumentid",
    "finledger :  document__id", "finledger::document_id", "finledger\t:\ndocument_ id", "FINLEDGER DOCUMENTID x",
    "finledger:docid", "finledger - document_id", "x finledger:document_id=1 finledger document id", "plain text", "",
    "finledger " + " " * 50 + ":" + " " * 50 + "document" + " " * 50 + "id",
])
def test_marker_like_matches_exactly_what_the_old_pattern_matched(text):
    assert [m.span() for m in _MARKER_LIKE.finditer(text)] == [m.span() for m in _OLD_MARKER_LIKE.finditer(text)]


@pytest.mark.parametrize("text", [
    "finledger" + " " * 200_000 + "x",
    "finledger" + " " * 200_000 + ":" + " " * 200_000 + "x",
    "finledger document" + " " * 200_000 + "x",
])
def test_marker_like_is_linear_on_long_whitespace_runs(text):
    started = time.perf_counter()
    assert _MARKER_LIKE.search(text) is None
    assert time.perf_counter() - started < 1.0


# ---------- 6. NUL characters ----------


@pytest.mark.parametrize("raw", [
    COMPANIES.decode("utf-8").encode("utf-16-le"),            # UTF-16 without BOM, read as UTF-8
    COMPANIES.replace(b"</NAME>", b"\x00</NAME>", 1),          # a stray NUL in UTF-8
    b"\xff\xfe" + COMPANIES.decode("utf-8").replace("</NAME>", "\x00</NAME>", 1).encode("utf-16-le"),
], ids=["utf16-no-bom", "utf8-nul", "utf16-bom-nul"])
def test_a_reply_holding_nul_characters_is_refused(raw):
    with pytest.raises(TallyMalformedResponse, match="NUL"):
        parse_company_list(raw)


def test_utf16_with_bom_still_parses():
    assert parse_company_list(b"\xff\xfe" + COMPANIES.decode("utf-8").encode("utf-16-le"))[0].guid == TEST_CO[1]
