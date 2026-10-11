"""Company identity, isolation and voucher-identity parsing: adversarial cases (mission Parts 7 and 11, parsing side).

Every Tally here is SYNTHETIC: the in-process FakeTally on 127.0.0.1, or reply bytes built from the REAL 2026-10-10
company list and voucher export (tests/fixtures/tallyprime_7_1_edu_run_20261010_162316). Real evidence behind the
rules tested: two companies were listed and each pinned voucher export held only its own vouchers (the second held
none); a pin to an unloaded company crashed TallyPrime (c0000005); voucher GUID = ``<company GUID>-<8 hex>``; MASTERID
stayed and ALTERID changed across an edit; the narration marker survived the edit; SVFROMDATE/SVTODATE were ignored.
Isolation stays PARTIALLY verified: nothing here is new real evidence.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    AgentSettings,
    CompanyNotResolved,
    LookupStatus,
    Phase,
    State,
    TallyCompany,
    TallyConnector,
    TallyMalformedResponse,
    TallyRequestRejected,
    TallyTransport,
    VoucherRow,
    find_by_marker,
    lookup_document,
    parse_company_list,
    parse_ledger_export,
    parse_voucher_export,
    post_approved,
    resolve_company,
)
from finledger_tally.company import assert_pinned_to, pinned_export
from finledger_tally.identity import name_key
from finledger_tally.lookup import voucher_request
from finledger_tally.transport import RequestRefused
from test_isolation_check import OTHER_CO, REJECTED, TEST_CO, company_list, voucher, vouchers_reply
from test_masters import BOOK, ledger, ledgers_reply
from test_posting import NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 - tally is a fixture

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
OTHER_DOC = "11111111-1111-4111-8111-111111111111"
G = TEST_CO[1]
NEW_GUID = "00000000-1111-4222-8333-444444444444"  # synthetic
DAY = date(2026, 4, 1)
MARKER = f"finledger:document_id={DOC}"


def T(fake, max_reply_bytes=64 * 1024 * 1024):
    return TallyTransport(port=fake.port, timeout=2, max_reply_bytes=max_reply_bytes)


def companies(*pairs) -> list[TallyCompany]:
    return parse_company_list(company_list(list(pairs)))


def with_narration(text: str, master_id: int = 1, company_guid: str = G, guid: str | None = None) -> str:
    block = voucher(company_guid, master_id, doc=None, guid=guid)
    return re.sub(r'<NARRATION TYPE="String">.*?</NARRATION>',
                  lambda _: f'<NARRATION TYPE="String">{escape(text)}</NARRATION>', block, flags=re.S)


def with_amount(amount: str) -> str:
    """The voucher block with its first (voucher-level) AMOUNT replaced; ledger-entry amounts are not read."""
    old = '<AMOUNT TYPE="Amount">1180.00</AMOUNT>'
    return voucher(G, 1).replace(old, f'<AMOUNT TYPE="Amount">{amount}</AMOUNT>', 1)


def rows_of(*blocks: str) -> list[VoucherRow]:
    return parse_voucher_export(vouchers_reply(list(blocks)))


def lookup(fake, company=TEST_CO, **kw):
    return lookup_document(T(fake, **kw), company[0], DOC, DAY, company[1])


def pin_of(body: bytes) -> str | None:
    match = re.search(rb"<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>", body)
    return match.group(1).decode() if match else None


class ShiftingCompanies(FakeTally):
    """Answers every company list after the first with ``later`` (bytes)."""

    def __init__(self, later: bytes, **kw):
        super().__init__(**kw)
        self.later = later

    def __call__(self, body, n):
        reply = super().__call__(body, n)
        if self.kinds[-1] == "Company" and self.kinds.count("Company") > 1:
            return self.later
        return reply


class RawVoucherReply(FakeTally):
    """Answers the pinned Voucher export with fixed bytes."""

    def __init__(self, reply: bytes, **kw):
        super().__init__(**kw)
        self.reply = reply

    def __call__(self, body, n):
        out = super().__call__(body, n)
        return self.reply if self.kinds[-1] == "Voucher" else out


class IgnoresPin(FakeTally):
    """A Tally that answers a pinned Voucher export with every loaded company's vouchers."""

    def __call__(self, body, n):
        out = super().__call__(body, n)
        if self.kinds[-1] == "Voucher":
            return vouchers_reply([b for blocks in self.vouchers.values() for b in blocks])
        return out


# ---------- company list and name resolution ----------


def test_duplicate_exact_names_resolve_neither_but_leave_other_names_resolvable():
    listed = companies(("Acme Traders", G), ("Acme Traders", OTHER_CO[1]), ("Other Co", NEW_GUID))
    with pytest.raises(CompanyNotResolved, match="2 loaded companies are named"):
        resolve_company(listed, "Acme Traders")
    assert resolve_company(listed, "Other Co").guid == NEW_GUID


@pytest.mark.parametrize("name,twin", [
    ("Acme Traders", "ACME TRADERS"),
    ("Acme Traders", "acme traders"),
    ("Acme Traders", "Acme  Traders"),                          # doubled space
    ("Acme Traders", " Acme Traders"),                          # leading space
    ("Acme Traders", "\uff21\uff43\uff4d\uff45 Traders"),       # full-width letters
    ("Strasse Co", "STRASSE CO"),
    ("Stra\u00dfe Co", "STRASSE CO"),                                # case folding, not just lower()
])
def test_case_or_spacing_collision_makes_even_the_exact_name_ambiguous(name, twin):
    listed = companies((name, G), (twin, OTHER_CO[1]))
    for exact in (name, twin):
        with pytest.raises(CompanyNotResolved, match="ambiguous"):
            resolve_company(listed, exact)


def test_a_case_variant_alone_is_not_the_company():
    with pytest.raises(CompanyNotResolved):
        resolve_company(companies(("ACME TRADERS", G)), "Acme Traders")


@pytest.mark.parametrize("other", ["Acme Traders Pvt", "Acme Trader", "Acme-Traders", "AcmeTraders"])
def test_merely_similar_names_do_not_block_resolution(other):
    listed = companies(("Acme Traders", G), (other, OTHER_CO[1]))
    assert resolve_company(listed, "Acme Traders") == TallyCompany("Acme Traders", G)


def test_name_key_folds_only_case_compatibility_forms_and_spacing():
    assert name_key("  Acme \t Traders ") == name_key("ACME TRADERS") == name_key("\uff21cme Traders")
    assert name_key("Acme Traders") != name_key("Acme-Traders") != name_key("AcmeTraders")


@pytest.mark.parametrize("bad", [None, 42, b"FinLedger Test Company"])
def test_resolve_refuses_a_name_that_is_not_a_string(bad):
    with pytest.raises(CompanyNotResolved):
        resolve_company(companies(TEST_CO), bad)


@pytest.mark.parametrize("second", [
    ("Copy Co", G),               # the same GUID twice
    ("Copy Co", G.upper()),       # the same GUID, differing only in case
    TEST_CO,                      # the same row twice
])
def test_company_list_with_duplicate_guids_fails_closed(second):
    with pytest.raises(TallyMalformedResponse, match="share one GUID"):
        parse_company_list(company_list([TEST_CO, second]))


@pytest.mark.parametrize("guid", [
    "7a5f42d4-ec1d-4aab-9992-e2e217bcb77",                      # short
    "7a5f42d4-ec1d-4aab-9992-e2e217bcb7733",                    # long
    "7a5f42d4ec1d4aab9992e2e217bcb773",                         # no hyphens
    "7a5f42d4-ec1d-4aab-9992-e2e217bcb77g",                     # not hex
    "7a5f42d4-ec1d-4aab-9992 e2e217bcb773",                     # space for a hyphen
    "7a5f42d4\u2010ec1d-4aab-9992-e2e217bcb773",                # Unicode hyphen
    "\uff17a5f42d4-ec1d-4aab-9992-e2e217bcb773",                # full-width digit
    "\u0667a5f42d4-ec1d-4aab-9992-e2e217bcb773",                # Arabic-Indic digit
    "{7a5f42d4-ec1d-4aab-9992-e2e217bcb773}",
    "7a5f42d4-ec1d-4aab-9992-e2e217bcb773-00000001",            # an object GUID, not a company GUID
    "",
])
def test_company_list_with_a_malformed_guid_fails_closed(guid):
    raw = company_list([TEST_CO]).replace(f">{G}<".encode(), f">{guid}<".encode())
    with pytest.raises(TallyMalformedResponse):
        parse_company_list(raw)


def test_company_list_with_a_missing_name_or_guid_fails_closed():
    for element in (f'<GUID TYPE="String">{G}</GUID>', f'<NAME TYPE="String">{TEST_CO[0]}</NAME>'):
        raw = company_list([TEST_CO]).replace(element.encode(), b"")
        with pytest.raises(TallyMalformedResponse, match="lacks NAME or GUID"):
            parse_company_list(raw)


def test_company_name_attribute_must_agree_with_the_name_element():
    raw = company_list([TEST_CO]).replace(f'NAME="{TEST_CO[0]}"'.encode(), b'NAME="Another Company"')
    with pytest.raises(TallyMalformedResponse, match="disagree"):
        parse_company_list(raw)
    assert parse_company_list(company_list([TEST_CO])) == [TallyCompany(*TEST_CO)]  # the real shape still parses


def test_a_company_row_outside_the_collection_is_refused():
    real = (REAL / "response_01_companies.xml").read_bytes()
    start = real.index(b"<COMPANY NAME")
    block = real[start:real.index(b"</COMPANY>", start) + len(b"</COMPANY>")]
    assert G.encode() in block
    raw = real.replace(b"</COLLECTION>", b"</COLLECTION>" + block.replace(G.encode(), OTHER_CO[1].encode()))
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_company_list(raw)
    assert len(parse_company_list(real)) == 1  # the CMPINFO <COMPANY>1</COMPANY> counter is not a row


@pytest.mark.parametrize("where", ["next-to-cmpinfo", "inside-cmpinfo", "second-desc", "lower-case-tag"])
def test_a_full_company_row_under_desc_or_in_lower_case_is_refused(where):
    real = (REAL / "response_01_companies.xml").read_bytes()
    start = real.index(b"<COMPANY NAME")
    block = real[start:real.index(b"</COMPANY>", start) + len(b"</COMPANY>")].replace(G.encode(), OTHER_CO[1].encode())
    raw = {"next-to-cmpinfo": lambda: real.replace(b"<CMPINFO>", block + b"<CMPINFO>", 1),
           "inside-cmpinfo": lambda: real.replace(b"<CMPINFO>", b"<CMPINFO>" + block, 1),
           "second-desc": lambda: real.replace(b"</DESC>", b"</DESC><DESC>" + block + b"</DESC>", 1),
           "lower-case-tag": lambda: real.replace(b"</COLLECTION>", b"</COLLECTION>" + block.replace(
               b"<COMPANY ", b"<company ").replace(b"</COMPANY>", b"</company>"))}[where]()
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_company_list(raw)


# ---------- pinned_export: resolve, pin exactly that company, re-resolve ----------


def test_pinned_export_pins_exactly_the_name_resolved_from_a_fresh_list(tally):
    fake = tally()
    company, raw = pinned_export(T(fake), TEST_CO[0], lambda c: voucher_request(c, DAY, DAY), TEST_CO[1])
    assert company == TallyCompany(*TEST_CO) and parse_voucher_export(raw) == []
    assert fake.kinds == ["Company", "Voucher", "Company"]
    assert [pin_of(b) for b in fake.server.received] == [None, TEST_CO[0], None]


@pytest.mark.parametrize("listed,expected_guid", [
    ([TEST_CO, (TEST_CO[0].upper(), OTHER_CO[1])], TEST_CO[1]),   # case-only collision, exact name loaded
    ([TEST_CO, (TEST_CO[0].upper(), OTHER_CO[1])], None),
    ([TEST_CO, (TEST_CO[0], OTHER_CO[1])], None),                 # duplicate exact name
    ([OTHER_CO], None),                                           # not loaded
    ([(TEST_CO[0].lower(), TEST_CO[1])], TEST_CO[1]),             # loaded only under another case
    ([(TEST_CO[0], OTHER_CO[1])], TEST_CO[1]),                    # same name, another GUID
    ([], None),
])
def test_an_unresolved_or_ambiguous_company_is_never_pinned(tally, listed, expected_guid):
    fake = tally(companies=listed)
    with pytest.raises(CompanyNotResolved):
        pinned_export(T(fake), TEST_CO[0], lambda c: voucher_request(c, DAY, DAY), expected_guid)
    assert fake.kinds == ["Company"] and len(fake.server.received) == 1


def _no_pin(c):
    return re.sub(rb"\s*<SVCURRENTCOMPANY>.*?</SVCURRENTCOMPANY>", b"", voucher_request(c, DAY, DAY))


def _pin_in_header(c):
    return _no_pin(c).replace(b"</HEADER>", f"<SVCURRENTCOMPANY>{c.name}</SVCURRENTCOMPANY></HEADER>".encode())


@pytest.mark.parametrize("build", [
    lambda c: voucher_request(TallyCompany("Unloaded Co", c.guid), DAY, DAY),
    lambda c: voucher_request(TallyCompany(c.name.upper(), c.guid), DAY, DAY),
    lambda c: voucher_request(TallyCompany(c.name + " ", c.guid), DAY, DAY),
    lambda c: voucher_request(TallyCompany(*OTHER_CO), DAY, DAY),       # a loaded company, but not the resolved one
    _no_pin,
    _pin_in_header,
    lambda c: voucher_request(c, DAY, DAY).replace(b"SVCURRENTCOMPANY", b"svcurrentcompany"),
    lambda c: voucher_request(c, DAY, DAY).replace(
        b"</STATICVARIABLES>", f"<SVCURRENTCOMPANY>{c.name}</SVCURRENTCOMPANY></STATICVARIABLES>".encode()),
    lambda c: b"<ENVELOPE>",
    lambda c: voucher_request(c, DAY, DAY).replace(b"</SVCURRENTCOMPANY>", b"<X/> Unloaded Co</SVCURRENTCOMPANY>"),
    lambda c: voucher_request(c, DAY, DAY).replace(b"<SVCURRENTCOMPANY>", b'<SVCURRENTCOMPANY TYPE="x">'),
    lambda c: voucher_request(c, DAY, DAY).replace(b"<STATICVARIABLES>", b'<STATICVARIABLES SVCURRENTCOMPANY="U">'),
    lambda c: voucher_request(c, DAY, DAY).replace(b"<ENVELOPE>", b'<ENVELOPE svCurrentCompany="Unloaded Co">'),
], ids=["unloaded", "case-variant", "padded", "other-loaded", "no-pin", "pin-in-header", "lower-case-tag",
        "two-pins", "not-xml", "child-in-pin", "attribute-on-pin", "pin-as-attribute", "pin-as-root-attribute"])
def test_a_request_not_pinned_to_the_resolved_company_is_never_sent(tally, build):
    fake = tally()
    with pytest.raises(RequestRefused, match="pinned"):
        pinned_export(T(fake), TEST_CO[0], build, TEST_CO[1])
    assert fake.kinds == ["Company"] and len(fake.server.received) == 1


@pytest.mark.parametrize("name", ["R&D <Traders> Import", "O'Brien \"Quoted\" Co", "Tab\u00a0Space Co"])
def test_escaped_names_are_accepted_as_their_exact_pin(name):
    company = TallyCompany(name, G)
    body = voucher_request(company, DAY, DAY)
    assert assert_pinned_to(body, company) is body
    with pytest.raises(RequestRefused):
        assert_pinned_to(body, TallyCompany(name + "x", G))


@pytest.mark.parametrize("later,error", [
    (company_list([(TEST_CO[0], NEW_GUID)]), CompanyNotResolved),                      # same name, new GUID
    (company_list([OTHER_CO]), CompanyNotResolved),                                    # unloaded
    (company_list([TEST_CO, (TEST_CO[0], OTHER_CO[1])]), CompanyNotResolved),          # a same-name twin appears
    (company_list([TEST_CO, (TEST_CO[0].upper(), OTHER_CO[1])]), CompanyNotResolved),  # a case twin appears
    (b"<ENVELOPE><BODY>", TallyMalformedResponse),
    (REJECTED, TallyRequestRejected),
], ids=["guid-swapped", "unloaded", "same-name-twin", "case-twin", "malformed", "rejected"])
@pytest.mark.parametrize("expected_guid", [TEST_CO[1], None], ids=["paired", "unpaired"])
def test_company_identity_changing_during_the_read_discards_the_reply(later, error, expected_guid):
    fake = ShiftingCompanies(later)
    try:
        with pytest.raises(error):
            pinned_export(T(fake), TEST_CO[0], lambda c: voucher_request(c, DAY, DAY), expected_guid)
        assert fake.kinds == ["Company", "Voucher", "Company"]
        assert fake.imports == []
    finally:
        fake.close()


def test_lookup_when_the_company_changes_mid_read_is_unreadable_never_not_found():
    fake = ShiftingCompanies(company_list([(TEST_CO[0], NEW_GUID)]))
    try:
        result, company = lookup(fake)
        assert result.status is LookupStatus.UNREADABLE and company is None and result.row is None
    finally:
        fake.close()


# ---------- the connector paths refuse an ambiguous company without pinning it ----------


CASE_TWINS = [TEST_CO, (TEST_CO[0].upper(), OTHER_CO[1])]


def test_lookup_with_a_case_twin_loaded_never_pins_and_is_unreadable(tally):
    fake = tally(companies=CASE_TWINS)
    fake.add_voucher(G, narration=MARKER)
    result, company = lookup(fake)
    assert result.status is LookupStatus.UNREADABLE and company is None and "ambiguous" in result.reason
    assert fake.kinds == ["Company"]


def test_posting_with_a_case_twin_loaded_never_imports(tally, tmp_path):
    fake = tally(companies=CASE_TWINS)
    j = make_job()
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and "company_unverified" in dict(out.problems)
    assert fake.imports == [] and set(fake.kinds) == {"Company"}
    assert phases(settings_for(fake, tmp_path)) == []


def test_health_and_connect_with_a_case_twin_loaded_are_not_ready(tally):
    fake = tally(companies=CASE_TWINS)
    settings = AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port, timeout=2,
                             posting_enabled=True, client_id="33333333-3333-4333-8333-333333333333")
    report = TallyConnector(settings).health()
    assert not report.read_only_available and report.posting == "enabled_but_not_ready"
    assert "CompanyNotResolved" in report.masters
    assert report.company_verified is False and report.status == "company_ambiguous"
    with pytest.raises(CompanyNotResolved):
        TallyConnector(settings).connect()
    assert set(fake.kinds) == {"Company"}  # never pinned


# ---------- company isolation of rows ----------


def test_populated_and_empty_company_are_each_read_alone(tally):
    fake = tally()
    fake.add_voucher(G, narration=MARKER)
    found, _ = lookup(fake)
    absent, company = lookup(fake, OTHER_CO)
    assert found.status is LookupStatus.FOUND and found.row.guid.startswith(G + "-")
    assert absent.status is LookupStatus.NOT_FOUND and "does not prove" in absent.reason
    assert company == TallyCompany(*OTHER_CO)


def test_a_tally_that_ignores_the_pin_makes_every_lookup_unreadable():
    fake = IgnoresPin()
    try:
        fake.add_voucher(G, narration=MARKER)
        fake.add_voucher(OTHER_CO[1], narration="another company's voucher")
        for company in (TEST_CO, OTHER_CO):
            result, _ = lookup(fake, company)
            assert result.status is LookupStatus.UNREADABLE and "not company-scoped" in result.reason
            assert result.row is None and result.rows == ()
    finally:
        fake.close()


@pytest.mark.parametrize("blocks", [
    lambda: [voucher(OTHER_CO[1], 1)],                                         # foreign voucher with our marker
    lambda: [voucher(G, 1), voucher(OTHER_CO[1], 2)],                          # ours, then a foreign one
    lambda: [voucher(OTHER_CO[1], 2), voucher(G, 1)],                          # foreign first
    lambda: [voucher(G, 1), voucher(OTHER_CO[1], 2, doc=OTHER_DOC)],           # foreign without our marker
    lambda: [voucher(G, 1), voucher(OTHER_CO[1], 2, guid=f"{OTHER_CO[1].upper()}-00000002")],
    lambda: [voucher(G, 1), voucher(G, 2, guid=f"{G[:-1]}4-00000002")],        # prefix one digit off
])
def test_foreign_company_candidates_are_never_returned(blocks):
    rows = rows_of(*blocks())
    result = find_by_marker(rows, DOC, G)
    assert result.status is LookupStatus.UNREADABLE and "not company-scoped" in result.reason
    assert result.row is None and result.rows == ()


@pytest.mark.parametrize("guid", [
    f"{G}-0000001",                         # 7 hex
    f"{G}-000000001",                       # 9 hex
    f"{G}-00000001-00000002",               # an extra segment
    f"{G}-0000000\uff11",                   # full-width digit
    f"{G}\u2010" + "00000001",              # Unicode hyphen
    f"{G} -00000001",                       # space before the suffix
    f"{G}-0000 0001",
    f"{{{G}-00000001}}",
    f"{G[:8]}{G[9:]}-00000001",             # company part without its first hyphen
])
def test_invalid_voucher_guids_are_unreadable(guid):
    rows = rows_of(voucher(G, 1, guid=guid))
    result = find_by_marker(rows, DOC, G)
    assert result.status is LookupStatus.UNREADABLE and "not <company GUID>-<8 hex>" in result.reason
    assert result.row is None and result.rows == ()


@pytest.mark.parametrize("bad", [f"{G}-00000001", f" {G[:-1]}", G.replace("-", ""), "\uff17" + G[1:], 0, b"x", None])
def test_an_invalid_company_guid_argument_is_refused_even_with_no_rows(bad):
    with pytest.raises(ValueError):
        find_by_marker([], DOC, bad)


@pytest.mark.parametrize("guid,error", [
    (f"{OTHER_CO[1]}-0000001a", CompanyNotResolved),            # another company's ledger
    (f"{OTHER_CO[1].upper()}-0000001A", CompanyNotResolved),
    (f"{G}", TallyMalformedResponse),                           # no suffix
    (f"{G}-0000001", TallyMalformedResponse),
    (f"{G}-0000001g", TallyMalformedResponse),
    (f"{G}-0000001a-00000001", TallyMalformedResponse),
    ("", TallyMalformedResponse),
])
def test_wrong_company_or_malformed_ledger_rows_refuse_the_whole_ledger_reply(guid, error):
    rows = BOOK + [ledger("Odd Ledger", "Sundry Creditors", guid=guid or " ")]
    with pytest.raises(error):
        parse_ledger_export(ledgers_reply(rows), TallyCompany(*TEST_CO))
    assert len(parse_ledger_export(ledgers_reply(BOOK), TallyCompany(*TEST_CO)).ledgers) == 4


def test_guid_suffix_is_not_interpreted_as_masterid_or_order():
    rows = rows_of(with_narration(f"x {MARKER}", master_id=1, guid=f"{G}-0000b420"),
                   with_narration("unrelated", master_id=7, guid=f"{G}-00000001"))
    result = find_by_marker(rows, DOC, G)
    assert result.status is LookupStatus.FOUND
    assert (result.row.guid, result.row.master_id) == (f"{G}-0000b420", 1)


# ---------- find_by_marker: classification ----------


def test_one_exact_marker_is_found_with_its_identity():
    result = find_by_marker(rows_of(voucher(G, 1), with_narration("plain", master_id=2)), DOC, G)
    assert result.status is LookupStatus.FOUND and result.rows == (result.row,)
    assert (result.row.guid, result.row.master_id, result.row.alter_id) == (f"{G}-00000001", 1, 3)


@pytest.mark.parametrize("narration", ["Test purchase", "", "finledger:document_id=" + OTHER_DOC])
def test_no_marker_for_this_document_is_not_found_and_says_it_is_not_proof(narration):
    result = find_by_marker(rows_of(with_narration(narration)), DOC, G)
    assert result.status is LookupStatus.NOT_FOUND and result.row is None
    assert "does not prove" in result.reason


def test_missing_narration_element_is_unreadable_not_missing():
    block = re.sub(r'<NARRATION TYPE="String">.*?</NARRATION>', "", voucher(G, 1))
    assert find_by_marker(rows_of(block), DOC, G).status is LookupStatus.UNREADABLE


def test_two_vouchers_with_the_marker_are_ambiguous_and_name_no_single_voucher():
    result = find_by_marker(rows_of(voucher(G, 1), voucher(G, 2)), DOC, G)
    assert result.status is LookupStatus.AMBIGUOUS and result.row is None
    assert {r.master_id for r in result.rows} == {1, 2}


@pytest.mark.parametrize("narration", [
    f"Test purchase finledger:document_id={DOC.upper()}",                       # upper-case id
    f"Test purchase FINLEDGER:DOCUMENT_ID={DOC}",                               # upper-case prefix
    f"Test purchase finledger:document_id={DOC}x",                              # extra character
    f"Test purchase finledger:document_id={DOC}-1",
    f"Test purchase finledger:document_id=x{DOC}",
    f"Test purchase finledger:document_id={{{DOC}}}",
    f"Test purchase finledger:document_id={DOC.replace('-', '')}",              # hyphens dropped
    f"Test purchase finledger:document_id={DOC[:18]}\n{DOC[18:]}",              # split across lines
    f"Test purchase finledger:document_id={DOC[:18]} {DOC[18:]}",
    f"Test purchase finledger:\ndocument_id={DOC}",
    f"Test purchase finledger:docu ment_id={DOC}",                              # split inside the word
    f"Test purchase document_id={DOC}",                                         # prefix lost
    f"Test purchase finledger.document_id={DOC}",
    f"Test purchase finledger:doc_id={DOC}",
    f"Test purchase {DOC}",                                                     # bare id
    f"Test purchase \uff46\uff49\uff4eledger:document_id={DOC}",                # full-width letters
    f"Test purchase \ufb01nledger:document_id={DOC}",                           # 'fi' ligature
    f"Test purchase finledger\u200b:document_id={DOC}",                         # zero-width space
    f"Test purchase finledger:document_id={DOC[:8]}\u200b{DOC[8:]}",
    f"finledger:document_id={OTHER_DOC} see {DOC}",                             # another marker, our id loose
    f"{MARKER} {MARKER}",
])
def test_altered_markers_of_this_document_never_match_and_are_unreadable(narration):
    result = find_by_marker(rows_of(with_narration(narration)), DOC, G)
    assert result.status is LookupStatus.UNREADABLE and result.row is None


@pytest.mark.parametrize("other", [
    "9d4f3d5d-499e-41db-b510-e98348e5b504",       # one digit off: a different canonical id
    "9d4f3d5d-499e-41db-b510-e98348e5b50a",
    OTHER_DOC,
])
def test_a_marker_with_a_different_uuid_is_another_document(other):
    result = find_by_marker(rows_of(with_narration(f"Test purchase finledger:document_id={other}")), DOC, G)
    assert result.status is LookupStatus.NOT_FOUND and result.row is None
    assert find_by_marker(rows_of(with_narration(f"x finledger:document_id={other}")), other, G).status \
        is LookupStatus.FOUND


@pytest.mark.parametrize("narration", [f"({MARKER})", f"{MARKER}.", f"Line one\r\n{MARKER}\r\nLine three",
                                       f"Invoice for {DOC} {MARKER}"])  # a draft narration may repeat its own id
def test_exact_marker_between_punctuation_or_lines_is_found(narration):
    assert find_by_marker(rows_of(with_narration(narration)), DOC, G).status is LookupStatus.FOUND


@pytest.mark.parametrize("alter", ["> 3</ALTERID>", "> 4</ALTERID>", "> 4000</ALTERID>", None])
def test_alterid_change_is_still_the_same_voucher(alter):
    block = voucher(G, 1)
    block = block.replace("> 3</ALTERID>", alter) if alter else re.sub(r"<ALTERID[^>]*>.*?</ALTERID>", "", block)
    result = find_by_marker(rows_of(block), DOC, G)
    assert result.status is LookupStatus.FOUND
    assert (result.row.guid, result.row.master_id) == (f"{G}-00000001", 1)
    assert result.row.alter_id == (int(alter[2:-10]) if alter else None)


def test_one_voucher_listed_twice_with_different_alterids_is_unreadable():
    twice = rows_of(voucher(G, 1), voucher(G, 1).replace("> 3</ALTERID>", "> 4</ALTERID>"))
    result = find_by_marker(twice, DOC, G)
    assert result.status is LookupStatus.UNREADABLE and "share guid" in result.reason


@pytest.mark.parametrize("second,why", [
    (lambda r: r, "guid"),                                                         # the very same row
    (lambda r: dataclasses.replace(r, master_id=2), "guid"),                       # same GUID, other MASTERID
    (lambda r: dataclasses.replace(r, guid=r.guid.upper(), master_id=2), "guid"),  # GUID differing only in case
    (lambda r: dataclasses.replace(r, guid=f"{G}-00000002", narration="x"), "master_id"),  # same MASTERID
])
def test_duplicate_guid_or_masterid_rows_are_unreadable_never_two_vouchers(second, why):
    [row] = rows_of(voucher(G, 1))
    result = find_by_marker([row, second(row)], DOC, G)
    assert result.status is LookupStatus.UNREADABLE and f"share {why}" in result.reason and result.row is None


def test_rows_may_be_any_iterable_but_only_voucher_rows():
    rows = rows_of(voucher(G, 1))
    assert find_by_marker(iter(rows), DOC, G).status is LookupStatus.FOUND  # not exhausted by the scope checks
    with pytest.raises(TypeError):
        find_by_marker([{"guid": f"{G}-00000001"}], DOC, G)
    with pytest.raises(TypeError):
        find_by_marker("not rows", DOC, G)


# ---------- voucher reply parsing: numbers, amounts, stray rows ----------


@pytest.mark.parametrize("tag", ["MASTERID", "ALTERID"])
@pytest.mark.parametrize("value", ["\u00b2", "\u0661", "\uff11", "-1", "+1", "1.0", "1e3", "1 2", "9" * 5000],
                         ids=["superscript", "arabic-indic", "full-width", "negative", "plus", "decimal", "exponent",
                              "inner-space", "5000-digits"])
def test_voucher_ids_must_be_plain_ascii_whole_numbers(tag, value):
    block = re.sub(rf"(<{tag} TYPE=\"Number\">)[^<]*(</{tag}>)", lambda m: m.group(1) + value + m.group(2),
                   voucher(G, 1))
    with pytest.raises(TallyMalformedResponse, match=tag):
        rows_of(block)


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity", "1e3", "1E+3", "1,180.00", "1180.00 Dr",
                                    "+1180.00", "\u0661\u0661\u0668\u0660", "1180.", ".50", "--1", "1180.00.00",
                                    "\uff11180.00"])
def test_voucher_amount_must_be_a_plain_decimal(amount):
    with pytest.raises(TallyMalformedResponse, match="AMOUNT"):
        rows_of(with_amount(amount))


@pytest.mark.parametrize("amount,expected", [("1180.00", Decimal("1180.00")), ("-1180.00", Decimal("-1180.00")),
                                             ("0", Decimal("0")), ("1180.005", Decimal("1180.005")), ("", None)])
def test_plain_amounts_are_exact_and_never_rounded(amount, expected):
    [row] = rows_of(with_amount(amount))
    assert row.amount == expected and (expected is None or str(row.amount) == amount)


@pytest.mark.parametrize("where", ["after-collection", "nested-in-row", "under-body", "in-header"])
def test_a_voucher_row_outside_the_collection_is_refused(where):
    stray = voucher(G, 1)
    raw = vouchers_reply([]).decode()
    raw = {"after-collection": lambda: raw.replace("</COLLECTION>", "</COLLECTION>" + stray),
           "nested-in-row": lambda: vouchers_reply([with_narration("x", master_id=2).replace(
               "</VOUCHER>", stray + "</VOUCHER>", 1)]).decode(),
           "under-body": lambda: raw.replace("</BODY>", stray + "</BODY>"),
           "in-header": lambda: raw.replace("</HEADER>", stray + "</HEADER>")}[where]()
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_voucher_export(raw.encode())


@pytest.mark.parametrize("where", ["next-to-cmpinfo", "inside-cmpinfo", "second-desc", "counter-with-child",
                                   "counter-not-a-number", "lower-case-tag"])
def test_a_marked_voucher_hidden_under_desc_is_refused_never_not_found(where):
    """A full row carrying this document's exact marker anywhere but the collection must not read as absent: a
    pre-Import lookup that says not_found would let the Import go ahead."""
    real = (REAL / "response_02_day_before_edit.xml").read_bytes().decode("utf-8")
    stray = with_narration(MARKER, master_id=9)
    raw = {"next-to-cmpinfo": lambda: real.replace("<CMPINFO>", stray + "<CMPINFO>", 1),
           "inside-cmpinfo": lambda: real.replace("<CMPINFO>", "<CMPINFO>" + stray, 1),
           "second-desc": lambda: real.replace("</DESC>", "</DESC><DESC>" + stray + "</DESC>", 1),
           "counter-with-child": lambda: real.replace("<VOUCHER>6</VOUCHER>", "<VOUCHER>6<X/></VOUCHER>", 1),
           "counter-not-a-number": lambda: real.replace("<VOUCHER>6</VOUCHER>", "<VOUCHER>six</VOUCHER>", 1),
           "lower-case-tag": lambda: real.replace("</COLLECTION>", "</COLLECTION>" + re.sub(
               r"(</?)VOUCHER\b", r"\1voucher", stray))}[where]()
    assert raw != real
    with pytest.raises(TallyMalformedResponse, match="outside the collection"):
        parse_voucher_export(raw.encode("utf-8"))
    fake = RawVoucherReply(raw.encode("utf-8"))
    try:
        result, _ = lookup(fake)
        assert result.status is LookupStatus.UNREADABLE and result.row is None
        assert fake.kinds == ["Company", "Voucher", "Company"] and fake.imports == []
    finally:
        fake.close()


def test_the_real_reply_with_its_cmpinfo_voucher_counter_still_parses():
    raw = (REAL / "response_02_day_before_edit.xml").read_bytes()
    assert b"<VOUCHER>6</VOUCHER>" in raw  # CMPINFO counter, not a row
    assert len(parse_voucher_export(raw)) == 1


# ---------- lookup: any doubt about the reply is unreadable, never not_found ----------


@pytest.mark.parametrize("reply", [
    b"",
    b"\xff\x00not xml",
    b"<ENVELOPE><BODY>",
    b'<!DOCTYPE ENVELOPE [<!ENTITY x "y">]><ENVELOPE/>',
    REJECTED,
    vouchers_reply([]).replace(b"</COLLECTION>", b"</COLLECTION><COLLECTION></COLLECTION>"),
    vouchers_reply([]).replace(b"</COLLECTION>", b"</COLLECTION>" + voucher(G, 1).encode()),
    vouchers_reply([voucher(G, 1).replace(">1180.00</AMOUNT>", ">sNaN</AMOUNT>", 1)]),
    vouchers_reply([voucher(G, 1).replace("> 1</MASTERID>", ">\u00b2</MASTERID>")]),
    vouchers_reply([voucher(G, 1)]).replace(b"<VOUCHER REMOTEID", b"<OBJECT REMOTEID").replace(
        b"</VOUCHER>", b"</OBJECT>"),
], ids=["empty", "not-utf8", "truncated", "dtd", "rejected", "two-collections", "stray-row", "nan-amount",
        "superscript-masterid", "unknown-row-tag"])
def test_malformed_voucher_reply_is_unreadable_never_not_found(reply):
    fake = RawVoucherReply(reply)
    try:
        result, _ = lookup(fake)
        assert result.status is LookupStatus.UNREADABLE and result.row is None
        assert fake.kinds == ["Company", "Voucher", "Company"] and fake.imports == []
    finally:
        fake.close()


def test_oversized_voucher_reply_is_unreadable_never_not_found():
    fake = RawVoucherReply(vouchers_reply([with_narration("filler " * 400, master_id=n) for n in range(1, 4)]))
    try:
        result, company = lookup(fake, max_reply_bytes=4096)
        assert result.status is LookupStatus.UNREADABLE and "ReplyTooLarge" in result.reason and company is None
    finally:
        fake.close()


def test_a_draft_narration_repeating_its_own_id_still_posts_and_is_found(tally, tmp_path):
    j = make_job(narration=f"Invoice for {NEW_DOC}")
    fake = tally()
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.POSTED and len(fake.imports) == 1 and out.erp_id.startswith(G + "-")


def test_unreadable_amount_after_an_import_is_uncertain_and_never_resent(tmp_path):
    class StoresNanAmount(FakeTally):
        def add_voucher(self, guid, *, amount="1180.00", **kw):
            out = super().add_voucher(guid, amount=amount, **kw)
            self.vouchers[guid][-1] = self.vouchers[guid][-1].replace(f">{amount}</AMOUNT>", ">sNaN</AMOUNT>", 1)
            return out

    fake = StoresNanAmount()
    try:
        j, s = make_job(), settings_for(fake, tmp_path)
        out = post_approved(j, approval(j), s)
        assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
        assert out.erp_result()["error"].startswith("outcome_uncertain")
        assert len(fake.imports) == 1 and phases(s)[-1] == Phase.OUTCOME_UNCERTAIN
        again = post_approved(j, approval(j), s)
        assert again.state == State.BLOCKED and len(fake.imports) == 1  # no resend
    finally:
        fake.close()
