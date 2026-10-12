"""Masters fetch and ledger mapping, hardened (mission Part 8).

The REAL TallyPrime EDU 7.1 ledger reply (fixtures/tallyprime_7_1_edu_masters_20261011_013427) is the baseline:
adversarial replies are that reply with one targeted change, or rows built in its exact shape (``NAME`` attribute,
no direct ``NAME`` child, ``LANGUAGENAME.LIST/NAME.LIST``, ``&#4; Primary``, ``<company GUID>-<8 hex>``). Every Tally
here is an in-process fake on 127.0.0.1; ``fake.kinds`` / ``fake.imports`` prove what was (not) sent.

Mission outcome -> existing ``LedgerStatus``: resolved -> EXISTS, missing -> MISSING, ambiguous -> AMBIGUOUS,
fetch_failed -> MASTERS_UNAVAILABLE (masters None), company_unverified -> COMPANY_UNVERIFIED; unsupported (tax, tds,
cost_centre mappings) -> ``ValueError`` from ``MappingTable.get``, never a status.
"""

from __future__ import annotations

import copy
import json
import pickle
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    CompanyNotResolved,
    LedgerMasters,
    LedgerStatus,
    MappingTable,
    State,
    TallyCompany,
    TallyMalformedResponse,
    TallyRequestRejected,
    fetch_ledger_masters,
    masters_snapshot_payloads,
    parse_ledger_export,
    post_approved,
    resolve_ledger,
    resolve_party,
)
from finledger_tally import voucher_xml
from finledger_tally.mapping import APPLIED_KINDS, MAPPING_KINDS
from finledger_tally.posting import load_masters_cache
from test_isolation_check import OTHER_CO, TEST_CO, company_list
from test_masters import ledger, ledgers_reply
from test_posting import CLIENT, approval, make_job, settings_for

HERE = Path(__file__).parent
REPO = HERE.parents[1]
REAL = HERE / "fixtures" / "tallyprime_7_1_edu_masters_20261011_013427"
LEDGERS = (REAL / "response_02_ledgers.xml").read_bytes()
CO = TallyCompany("FinLedger Test Company", "7a5f42d4-ec1d-4aab-9992-e2e217bcb773")
assert CO == TallyCompany(*TEST_CO)
VENDOR, PURCHASE = "FinLedger Test Vendor", "FinLedger Test Purchase"
GSTIN = "27AAPFU0939F1ZV"

_OPEN = b'<COLLECTION ISMSTDEPTYPE="Yes" MSTDEPTYPE="8">\r\n'
_HEAD, _TAIL = LEDGERS[:LEDGERS.index(_OPEN) + len(_OPEN)], LEDGERS[LEDGERS.index(b"   </COLLECTION>"):]
REAL_ROWS = re.findall(rb"    <LEDGER .*?</LEDGER>\r\n", LEDGERS, re.S)


def row(name_attr: str, parent: str, suffix: int, names: list[str] | None = None, guid: str | None = None) -> bytes:
    """One LEDGER row in the real reply's exact shape. ``name_attr``/``names``/``parent`` are raw XML text."""
    listed = "".join(f"       <NAME>{n}</NAME>\r\n" for n in (names if names is not None else [name_attr]))
    return (f'    <LEDGER NAME="{name_attr}" RESERVEDNAME="">\r\n'
            f'     <GUID TYPE="String">{guid or f"{CO.guid}-{suffix:08x}"}</GUID>\r\n'
            f'     <PARENT TYPE="String">{parent}</PARENT>\r\n     <LANGUAGENAME.LIST>\r\n'
            f'      <NAME.LIST TYPE="String">\r\n{listed}      </NAME.LIST>\r\n'
            f'      <LANGUAGEID TYPE="Number"> 1033</LANGUAGEID>\r\n     </LANGUAGENAME.LIST>\r\n'
            f'    </LEDGER>\r\n').encode("utf-8")


def reply(*extra: bytes, rows: list[bytes] | None = None) -> bytes:
    """The real reply's envelope around ``rows`` (default: the four real rows) plus ``extra`` rows."""
    return _HEAD + b"".join((REAL_ROWS if rows is None else rows) + list(extra)) + _TAIL


def edit(old: bytes, new: bytes) -> bytes:
    assert LEDGERS.count(old) == 1, old
    return LEDGERS.replace(old, new)


def table(*rows: tuple[str, str, str]) -> MappingTable:
    return MappingTable.from_rows([{"kind": k, "our_key": key, "erp_value": v} for k, key, v in rows])


REAL_MASTERS = parse_ledger_export(LEDGERS, CO)
# The real book plus one alias, "Purchases", of the purchase ledger (aliases were never seen in a real reply).
ALIASED = parse_ledger_export(edit(b"<NAME>FinLedger Test Purchase</NAME>",
                                   b"<NAME>FinLedger Test Purchase</NAME><NAME>Purchases</NAME>"), CO)


@pytest.fixture
def served():
    fakes: list[FakeTally] = []

    def start(cls=FakeTally, **kw):
        fake = cls(**kw)
        fakes.append(fake)
        return fake

    yield start
    for fake in fakes:
        fake.close()


class LedgerReplyTally(FakeTally):
    """FakeTally that answers the pinned Ledger export with fixed bytes (after its own pin checks)."""

    def __init__(self, ledger_reply: bytes, **kw):
        super().__init__(**kw)
        self.ledger_reply = ledger_reply

    def __call__(self, body: bytes, n: int) -> bytes:
        out = super().__call__(body, n)
        return self.ledger_reply if self.kinds[-1] == "Ledger" else out


class SecondListTally(FakeTally):
    """FakeTally whose SECOND company-list reply (the re-check after the ledger read) is replaced."""

    def __init__(self, second_list: bytes, **kw):
        super().__init__(**kw)
        self.second_list = second_list

    def __call__(self, body: bytes, n: int) -> bytes:
        out = super().__call__(body, n)
        return self.second_list if self.kinds.count("Company") == 2 and self.kinds[-1] == "Company" else out


def fetch(fake: FakeTally, name: str = CO.name, guid: str | None = CO.guid) -> LedgerMasters:
    return fetch_ledger_masters(name, port=fake.port, timeout=5, expected_guid=guid)


# ---------- the real reply is the baseline ----------


def test_real_reply_baseline_names_parents_and_the_cmpinfo_counter_is_not_a_row():
    assert b"<LEDGER>0</LEDGER>" in LEDGERS  # DESC/CMPINFO counter, same tag as the rows
    assert REAL_MASTERS.ledgers == {"Cash", PURCHASE, VENDOR, "Profit & Loss A/c"}
    assert REAL_MASTERS.parent_of("Profit & Loss A/c") == "Primary" and REAL_MASTERS.parent_of("Cash") == "Cash-in-Hand"
    assert REAL_MASTERS.aliases == () and REAL_MASTERS.company_guid == CO.guid
    assert parse_ledger_export(reply(), CO) == REAL_MASTERS  # the row builder below reproduces the real rows


def test_real_ledger_guids_are_read_case_insensitively_and_never_reinterpreted():
    upper = re.sub(rb"(<GUID TYPE=\"String\">)([^<]+)", lambda m: m.group(1) + m.group(2).upper(), LEDGERS)
    assert upper != LEDGERS and parse_ledger_export(upper, CO) == REAL_MASTERS


@pytest.mark.parametrize("guid,error", [
    (f"{CO.guid}-0000001", TallyMalformedResponse),             # 7 hex
    (f"{CO.guid}-000000001", TallyMalformedResponse),           # 9 hex
    (f"{CO.guid}-0000001g", TallyMalformedResponse),            # not hex
    (f"{{{CO.guid}-0000001f}}", TallyMalformedResponse),        # braces
    (f"{CO.guid}-0000001f-0000001f", TallyMalformedResponse),   # two suffixes
    (CO.guid, TallyMalformedResponse),                          # the company GUID itself
    (f"{OTHER_CO[1]}-0000001f", CompanyNotResolved),            # another company's ledger
])
def test_a_ledger_guid_that_is_not_this_company_plus_8_hex_refuses_the_reply(guid, error):
    raw = edit(f"{CO.guid}-0000001f".encode(), guid.encode())
    with pytest.raises(error):
        parse_ledger_export(raw, CO)


@pytest.mark.parametrize("dup", [b"-000000ce", b"-000000CE"], ids=["same", "same-ignoring-case"])
def test_duplicate_ledger_guids_refuse_the_reply(dup):
    with pytest.raises(TallyMalformedResponse, match="appears twice"):
        parse_ledger_export(edit(b"-000000cf</GUID>", dup + b"</GUID>"), CO)


# ---------- names: entities, Unicode, encodings ----------


@pytest.mark.parametrize("attr,listed,name", [
    ("R&amp;D &lt;Supplies&gt;", "R&amp;D &lt;Supplies&gt;", "R&D <Supplies>"),
    ("O&apos;Brien &quot;Bros&quot;", "O'Brien \"Bros\"", "O'Brien \"Bros\""),
    ("A &#38; B", "A &#x26; B", "A & B"),
    ("&#2344;&#2325;&#2342; &#8377;", "\u0928\u0915\u0926 \u20b9", "\u0928\u0915\u0926 \u20b9"),
    ("Tom &amp;amp; Jerry", "Tom &amp;amp; Jerry", "Tom &amp; Jerry"),
])
def test_xml_entities_in_names_are_decoded_exactly_once(attr, listed, name):
    m = parse_ledger_export(reply(row(attr, "Sundry Creditors", 0x40, [listed])), CO)
    assert name in m.ledgers
    assert resolve_ledger(name, m, CO.guid).status is LedgerStatus.EXISTS
    if attr != name:  # the escaped spelling is not the ledger's name
        assert resolve_ledger(attr, m, CO.guid).status is not LedgerStatus.EXISTS


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_multibyte_names_survive_every_encoding_tally_may_use(encoding):
    names = ["\u092e\u0947\u0938\u0930\u094d\u0938 \u0936\u0930\u094d\u092e\u093e "
             "\u091f\u094d\u0930\u0947\u0921\u0930\u094d\u0938", "\u20b9 Round Off",
             "\u0bb8\u0bcd\u0bb0\u0bc0 \u0bae\u0bc1\u0bb0\u0bc1\u0b95\u0ba9\u0bcd "
             "\u0bb8\u0bcd\u0b9f\u0bc0\u0bb2\u0bcd\u0bb8\u0bcd"]
    raw = reply(*(row(n, "Sundry Creditors", 0x50 + i) for i, n in enumerate(names)))
    m = parse_ledger_export(raw.decode("utf-8").encode(encoding), CO)
    assert m == parse_ledger_export(raw, CO) and set(names) <= m.ledgers
    assert all(resolve_party(n, m, CO.guid).tally_name == n for n in names)
    sent = masters_snapshot_payloads(m)[1]["items"]
    assert {"name": "\u20b9 Round Off", "parent": "Sundry Creditors"} in sent


@pytest.mark.parametrize("raw", [
    reply(row("Caf\u00e9", "Sundry Creditors", 0x40)).replace("Caf\u00e9".encode(), "Caf\u00e9".encode("cp1252")),
    reply(row("\u20b9 Round Off", "Indirect Expenses", 0x40)).replace("\u20b9".encode(), "\u20b9".encode()[:2]),
    reply().decode().encode("utf-16-le"),  # UTF-16 without a BOM
], ids=["cp1252", "truncated-utf8", "utf16-no-bom"])
def test_undecodable_bytes_refuse_the_reply_instead_of_guessing(raw):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(raw, CO)


@pytest.mark.parametrize("bad", ["Cash&#9;", "Cash&#10;Book", "Cash&#13;", "Ca&#4;sh", "Cash&#0;", "Cash&#127;",
                                 "Cash&#x85;", "Cash&nbsp;"])
def test_control_characters_and_undefined_entities_in_names_refuse_the_reply(bad):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(reply(row(bad, "Cash-in-Hand", 0x40)), CO)


def test_a_dtd_or_entity_declaration_is_refused_before_parsing():
    raw = b'<!DOCTYPE ENVELOPE [<!ENTITY v "FinLedger Test Vendor">]>' + LEDGERS.replace(
        b"<NAME>FinLedger Test Vendor</NAME>", b"<NAME>&v;</NAME>")
    with pytest.raises(TallyMalformedResponse, match="DTD"):
        parse_ledger_export(raw, CO)


@pytest.mark.parametrize("brk", ["\n", "\r\n", "\t"], ids=["lf", "crlf", "tab"])
@pytest.mark.parametrize("shape", ["real", "name-attribute-only"])
def test_a_raw_line_break_or_tab_inside_a_name_attribute_is_refused_not_read_as_a_space(brk, shape):
    # XML attribute normalisation would read NAME="Cash<brk>Book" as "Cash Book": a ledger Tally does not have.
    if shape == "real":
        raw = reply(row(f"Cash{brk}Book", "Cash-in-Hand", 0x40, ["Cash Book"]))
    else:
        raw = ledgers_reply([ledger(f"Cash{brk}Book", "Cash-in-Hand")])
    with pytest.raises(TallyMalformedResponse, match="raw tab or line break"):
        parse_ledger_export(raw, CO)


def test_line_breaks_between_attributes_are_ordinary_xml_and_accepted():
    raw = edit(b'<LEDGER NAME="Cash" RESERVEDNAME="">', b'<LEDGER\r\n     NAME="Cash"\r\n     RESERVEDNAME="">')
    assert parse_ledger_export(raw, CO) == REAL_MASTERS


# ---------- duplicates and look-alikes ----------


@pytest.mark.parametrize("a,b", [("Cash Book", "CASH BOOK"), ("Stra\u00dfe", "STRASSE"),
                                 ("\u00c4rzte Fees", "\u00e4rzte fees"),
                                 ("\u03a3\u038a\u03a3\u03a5\u03a6\u039f\u03a3",
                                  "\u03c3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2")])
def test_names_equal_ignoring_case_refuse_the_whole_reply(a, b):
    """Fail closed: Tally master names are unique regardless of case, so a reply holding both is not one company's
    genuine ledger list; resolving either name would mean choosing one, so neither the reply nor any of its ledgers
    is used."""
    with pytest.raises(TallyMalformedResponse, match="appears twice"):
        parse_ledger_export(reply(row(a, "Cash-in-Hand", 0x40), row(b, "Cash-in-Hand", 0x41)), CO)


@pytest.mark.parametrize("twin", [
    "FinLedger\u00a0Test Purchase",                                   # no-break space
    "FinLedger  Test Purchase",                                       # two spaces
    "FinLedger Test Purchase\u200b",                                  # zero-width space
    "FinLedger Test Purchase\u202e",                                  # bidi override
    "\uff26\uff49\uff4e\uff2c\uff45\uff44\uff47\uff45\uff52 Test Purchase",  # full-width letters
], ids=["nbsp", "double-space", "zero-width", "bidi", "full-width"])
def test_look_alike_names_parse_but_neither_twin_is_ever_chosen(twin):
    m = parse_ledger_export(reply(row(twin, "Purchase Accounts", 0x40)), CO)
    assert {PURCHASE, twin} <= m.ledgers
    for name in (PURCHASE, twin):
        r = resolve_ledger(name, m, CO.guid)
        assert r.status is LedgerStatus.AMBIGUOUS and r.tally_name is None and "look-alike" in r.detail
    assert resolve_party(VENDOR, m, CO.guid).status is LedgerStatus.EXISTS  # only the twins are affected


def test_unicode_composed_and_decomposed_twins_are_ambiguous():
    nfc, nfd = "Caf\u00e9 Supplies", unicodedata.normalize("NFD", "Caf\u00e9 Supplies")
    m = parse_ledger_export(reply(row(nfc, "Sundry Creditors", 0x40), row(nfd, "Sundry Creditors", 0x41)), CO)
    assert nfc != nfd and {nfc, nfd} <= m.ledgers
    assert {resolve_party(n, m, CO.guid).status for n in (nfc, nfd)} == {LedgerStatus.AMBIGUOUS}


def test_cached_masters_that_skip_the_parser_get_the_same_protection(tmp_path):
    """posting.load_masters_cache rebuilds masters from a JSON file without the parser's checks; resolution must not
    rely on the parser alone."""
    path = tmp_path / "masters.json"
    path.write_text(json.dumps({"company_name": CO.name, "company_guid": CO.guid, "fetched_at": "x",
                                "parents": [["Cash", "Cash-in-Hand"], ["CASH", "Cash-in-Hand"],
                                            [VENDOR, "Sundry Creditors"], ["Acme", "Sundry Creditors"]],
                                "aliases": [["Acme", VENDOR]]}), encoding="utf-8")
    masters, _ = load_masters_cache(path, CO)
    for name in ("Cash", "CASH", "Acme"):
        assert resolve_ledger(name, masters, CO.guid).status is LedgerStatus.AMBIGUOUS, name
    assert resolve_party(VENDOR, masters, CO.guid).status is LedgerStatus.EXISTS


# ---------- aliases ----------


@pytest.mark.parametrize("resolve", [
    lambda: resolve_ledger("Purchases", ALIASED, CO.guid),
    lambda: resolve_ledger("PURCHASES", ALIASED, CO.guid),
    lambda: resolve_party("Purchases", ALIASED, CO.guid),
    lambda: resolve_ledger("Purchase - RM", ALIASED, CO.guid, table(("ledger", "Purchase - RM", "Purchases"))),
    lambda: resolve_party("Purchases", ALIASED, CO.guid, table(("party", GSTIN, "Purchases")), GSTIN),
], ids=["direct", "case", "as-party", "ledger-mapping-to-alias", "gstin-mapping-to-alias"])
def test_an_alias_is_never_authoritative(resolve):
    r = resolve()
    assert r.status is LedgerStatus.AMBIGUOUS and r.tally_name is None and "alias" in r.detail
    assert resolve_ledger(PURCHASE, ALIASED, CO.guid).status is LedgerStatus.EXISTS


def test_an_alias_spelling_its_own_ledger_in_another_case_is_kept_but_never_matched():
    m = parse_ledger_export(edit(b"<NAME>Cash</NAME>", b"<NAME>Cash</NAME><NAME>CASH</NAME>"), CO)
    assert m.aliases == (("CASH", "Cash"),)
    assert resolve_ledger("Cash", m, CO.guid).tally_name == "Cash"
    assert resolve_ledger("CASH", m, CO.guid).status is LedgerStatus.AMBIGUOUS


# ---------- empty and malformed replies ----------


@pytest.mark.parametrize("raw", [
    reply(rows=[]),
    _HEAD.replace(_OPEN, b"<COLLECTION/>\r\n") + _TAIL[_TAIL.index(b"</COLLECTION>") + 13:],
    LEDGERS[:LEDGERS.index(b"  <DATA>")] + LEDGERS[LEDGERS.index(b"</DATA>") + 7:],
    b"", b"  \r\n", b"\xef\xbb\xbf",
], ids=["empty-collection", "self-closing-collection", "no-data", "zero-bytes", "whitespace", "bom-only"])
def test_there_is_no_valid_empty_ledger_reply(raw):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(raw, CO)


def test_an_empty_ledger_reply_over_the_wire_raises_and_never_imports(served):
    fake = served(ledgers={CO.guid: []})
    with pytest.raises(TallyMalformedResponse, match="no ledgers"):
        fetch(fake)
    assert fake.kinds == ["Company", "Ledger", "Company"] and fake.imports == []


_STRAY = row("Ghost Vendor", "Sundry Creditors", 0x99).strip()


@pytest.mark.parametrize("raw", [
    edit(b"  <DATA>\r\n", b"  <DATA>\r\n" + _STRAY),
    edit(b'<PARENT TYPE="String">Cash-in-Hand</PARENT>', b'<PARENT TYPE="String">Cash-in-Hand</PARENT>' + _STRAY),
    edit(b"<NAME>Cash</NAME>", b"<NAME>Cash</NAME>" + _STRAY),
    edit(b"</DATA>", b"</DATA>" + _STRAY),
], ids=["in-data", "nested-in-row", "in-name-list", "after-data"])
def test_a_ledger_element_outside_the_collection_rows_refuses_the_reply(raw):
    with pytest.raises(TallyMalformedResponse, match="outside the collection rows"):
        parse_ledger_export(raw, CO)


@pytest.mark.parametrize("raw", [
    edit(b"-0000001f</GUID>", b"-0000001f</GUID><GUID>" + CO.guid.encode() + b"-00000077</GUID>"),
    edit(b"Cash-in-Hand</PARENT>", b"Cash-in-Hand</PARENT><PARENT>Bank Accounts</PARENT>"),
    edit(b'<LEDGER NAME="Cash"', b'<LEDGER NAME=""').replace(b"<NAME>Cash</NAME>", b"<NAME></NAME>"),
    edit(b"<NAME>Cash</NAME>", b"<NAME>Cash</NAME><NAME></NAME>"),
    edit(b"    </LEDGER>\r\n   </COLLECTION>", b"    </LEDGER>\r\n<GROUP NAME=\"x\"/>   </COLLECTION>"),
], ids=["two-guids", "two-parents", "blank-name", "blank-alias", "stranger-in-collection"])
def test_malformed_rows_refuse_the_reply(raw):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(raw, CO)


# ---------- parent groups ----------


@pytest.mark.parametrize("parent", ["", "   ", " Cash-in-Hand", "Cash-in-Hand ", "&#4;", "&#4; Primary ", "&#4;Primary",
                                    "<X/>"])
def test_a_missing_blank_or_unreadable_parent_refuses_the_reply(parent):
    with pytest.raises(TallyMalformedResponse):
        parse_ledger_export(edit(b">Cash-in-Hand</PARENT>", f">{parent}</PARENT>".encode()), CO)


def test_a_missing_parent_element_refuses_the_reply():
    with pytest.raises(TallyMalformedResponse, match="has no PARENT"):
        parse_ledger_export(edit(b'     <PARENT TYPE="String">Cash-in-Hand</PARENT>\r\n', b""), CO)


def test_a_parent_group_that_was_not_fetched_is_reported_verbatim_never_checked_or_invented():
    """Groups are not fetched (FETCH NAME, PARENT, GUID), so the parent's existence cannot be verified here; it is
    carried exactly as Tally wrote it and resolution does not depend on it."""
    m = parse_ledger_export(reply(row("Acme Steel", "Sundry Creditors - Mumbai", 0x40)), CO)
    assert m.parent_of("Acme Steel") == "Sundry Creditors - Mumbai"
    assert {"name": "Acme Steel", "parent": "Sundry Creditors - Mumbai"} in masters_snapshot_payloads(m)[1]["items"]
    assert resolve_party("Acme Steel", m, CO.guid).status is LedgerStatus.EXISTS


# ---------- envelopes and Tally rejections ----------


@pytest.mark.parametrize("raw,error", [
    (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", TallyMalformedResponse),
    (edit(b"  <STATUS>1</STATUS>\r\n", b""), TallyRequestRejected),
    (edit(b"<STATUS>1</STATUS>", b"<STATUS>0</STATUS>"), TallyRequestRejected),
    (edit(b"<STATUS>1</STATUS>", b"<STATUS>1.0</STATUS>"), TallyRequestRejected),
    (edit(b"  <DATA>\r\n", b"  <DATA>\r\n<LINEERROR>Could not find Company</LINEERROR>"), TallyRequestRejected),
    (edit(b"   </COLLECTION>", b"   </COLLECTION><COLLECTION/>"), TallyMalformedResponse),
    (LEDGERS[:LEDGERS.index(b" <BODY>")] + b"</ENVELOPE>", TallyMalformedResponse),
], ids=["not-an-envelope", "no-status", "status-0", "status-1.0", "lineerror", "two-collections", "no-body"])
def test_envelope_problems_refuse_the_reply(raw, error):
    with pytest.raises(error):
        parse_ledger_export(raw, CO)


@pytest.mark.parametrize("answer,error", [
    (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>"
     b"Could not set &apos;SVCurrentCompany&apos;</LINEERROR></DATA></BODY></ENVELOPE>", TallyRequestRejected),
    (edit(b"<STATUS>1</STATUS>", b"<STATUS>0</STATUS>"), TallyRequestRejected),
    (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", TallyMalformedResponse),
], ids=["lineerror", "status-0", "unknown-request"])
def test_a_tally_rejection_of_the_ledger_export_raises_and_never_imports(served, answer, error):
    fake = served(LedgerReplyTally, ledger_reply=answer)
    with pytest.raises(error):
        fetch(fake)
    assert fake.kinds == ["Company", "Ledger", "Company"] and fake.imports == []


def test_the_real_reply_over_the_wire_is_accepted(served):
    fake = served(LedgerReplyTally, ledger_reply=LEDGERS)
    assert fetch(fake) == REAL_MASTERS and fake.kinds == ["Company", "Ledger", "Company"]


# ---------- company identity between the reads ----------


@pytest.mark.parametrize("second,error", [
    (company_list([(CO.name, "00000000-1111-4222-8333-444444444444"), OTHER_CO]), CompanyNotResolved),
    (company_list([(CO.name + " (old)", CO.guid), OTHER_CO]), CompanyNotResolved),
    (company_list([OTHER_CO]), CompanyNotResolved),
    (company_list([TEST_CO, (CO.name, OTHER_CO[1])]), CompanyNotResolved),
    (company_list([TEST_CO]).replace(CO.guid.encode(), b"not-a-guid"), TallyMalformedResponse),
    (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER></ENVELOPE>", TallyRequestRejected),
], ids=["guid-swapped", "renamed", "unloaded", "second-same-name", "malformed-list", "rejected-list"])
def test_a_company_change_between_the_reads_discards_the_ledgers(served, second, error):
    fake = served(SecondListTally, second_list=second)
    with pytest.raises(error):
        fetch(fake)
    assert fake.kinds == ["Company", "Ledger", "Company"] and fake.imports == []


def test_a_same_named_company_with_another_guid_is_never_pinned(served):
    fake = served(companies=[(CO.name, OTHER_CO[1])])
    with pytest.raises(CompanyNotResolved, match="paired"):
        fetch(fake)
    assert fake.kinds == ["Company"]


@pytest.mark.parametrize("guid", [OTHER_CO[1], CO.guid.upper(), f" {CO.guid}", "", None])
def test_masters_of_one_company_never_resolve_for_another(guid):
    mapping = table(("ledger", "Purchase - RM", PURCHASE), ("party", GSTIN, VENDOR))
    for r in (resolve_ledger(PURCHASE, REAL_MASTERS, guid),
              resolve_ledger("Purchase - RM", REAL_MASTERS, guid, mapping),
              resolve_party(VENDOR, REAL_MASTERS, guid, mapping, GSTIN)):
        assert r.status is LedgerStatus.COMPANY_UNVERIFIED and r.tally_name is None


def test_an_unverified_guid_never_verifies_itself():
    masters = LedgerMasters("not-a-guid", frozenset({"Cash"}))
    assert resolve_ledger("Cash", masters, "not-a-guid").status is LedgerStatus.COMPANY_UNVERIFIED


def test_no_masters_is_masters_unavailable_whatever_the_mapping_says():
    mapping = table(("ledger", "Purchase - RM", PURCHASE), ("party", GSTIN, VENDOR))
    assert resolve_ledger("Purchase - RM", None, CO.guid, mapping).status is LedgerStatus.MASTERS_UNAVAILABLE
    assert resolve_party(VENDOR, None, CO.guid, mapping, GSTIN).status is LedgerStatus.MASTERS_UNAVAILABLE


# ---------- MappingTable.from_rows against the P2 mapping_table shape ----------


def test_mapping_kinds_are_exactly_person2s_check_constraint_and_api_literal():
    sql = (REPO / "person2_platform/src/finledger_platform/migrations/002_documents_queue.sql").read_text()
    block = re.search(r"create table mapping_table \((.*?)\n\);", sql, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", re.search(r"check \(kind in \(([^)]*)\)\)", block).group(1))) == MAPPING_KINDS
    api = (REPO / "person2_platform/src/finledger_platform/api.py").read_text()
    literal = re.search(r"class MappingIn\(BaseModel\):\s*kind: Literal\[([^\]]*)\]", api).group(1)
    assert tuple(re.findall(r'"(\w+)"', literal)) == MAPPING_KINDS
    assert set(APPLIED_KINDS) == {"party", "ledger"}


def test_person2_rows_are_read_as_stored():
    client = uuid.UUID(CLIENT)
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = [{"id": i, "client_id": client, "kind": kind, "our_key": key, "erp_value": value, "updated_at": now}
            for i, (kind, key, value) in enumerate([("party", GSTIN, VENDOR), ("ledger", "Purchase - RM", PURCHASE),
                                                    ("tax", "cgst_9", "Input CGST 9%"), ("tds", "194C", "TDS 194C"),
                                                    ("cost_centre", "Plant 1", "Plant 1")])]
    t = MappingTable.from_rows(rows, client_id=CLIENT)
    assert t.get("party", GSTIN) == VENDOR and t.get("ledger", "Purchase - RM") == PURCHASE
    assert t.get("ledger", "purchase - rm") is None and t.rows["tax"] == {"cgst_9": "Input CGST 9%"}


@pytest.mark.parametrize("kind", ["gst", "Ledger", "LEDGER", " ledger", "", None, 1, ["ledger"]])
def test_unknown_mapping_kinds_are_refused(kind):
    with pytest.raises(ValueError, match="not one of"):
        MappingTable.from_rows([{"kind": kind, "our_key": "a", "erp_value": "b"}])


@pytest.mark.parametrize("bad", ["", " ", " a", "a ", "a\n", "\t", "a\x00b", None, 5, b"a"])
@pytest.mark.parametrize("column", ["our_key", "erp_value"])
def test_blank_padded_or_non_text_mapping_values_are_refused(column, bad):
    good = {"kind": "ledger", "our_key": "a", "erp_value": "b"}
    with pytest.raises(ValueError):
        MappingTable.from_rows([{**good, column: bad}])
    with pytest.raises(ValueError):
        MappingTable.from_rows([{k: v for k, v in good.items() if k != column}])


def test_a_duplicate_key_is_refused_even_with_the_same_value():
    with pytest.raises(ValueError, match="appears twice"):
        table(("ledger", "a", "b"), ("ledger", "a", "b"))


def test_the_same_key_in_two_kinds_or_two_cases_is_not_a_duplicate():
    t = table(("ledger", "Steel", "A"), ("party", "Steel", "B"), ("ledger", "steel", "C"))
    assert (t.get("ledger", "Steel"), t.get("party", "Steel"), t.get("ledger", "steel")) == ("A", "B", "C")


@pytest.mark.parametrize("rows", [
    [("ledger", "a", "b")], "ledger", b"ledger", {"kind": "ledger", "our_key": "a", "erp_value": "b"}, None, 5, [None],
], ids=["tuple-rows", "str", "bytes", "single-row-dict", "none", "int", "none-row"])
def test_mapping_rows_must_be_an_iterable_of_row_mappings(rows):
    with pytest.raises(TypeError):
        MappingTable.from_rows(rows)


def _r(client, key="a"):
    return {"kind": "ledger", "our_key": key, "erp_value": "b", **({} if client is ... else {"client_id": client})}


@pytest.mark.parametrize("rows,client_id", [
    ([_r(CLIENT), _r("44444444-4444-4444-8444-444444444444", "z")], None),
    ([_r(CLIENT)], "44444444-4444-4444-8444-444444444444"),
    ([_r(...)], CLIENT),
    ([_r(CLIENT), _r(..., "z")], None),
    ([_r(CLIENT), _r(None, "z")], None),
    ([_r("abcdef01-2345-4678-89ab-cdef01234567".upper())], "abcdef01-2345-4678-89ab-cdef01234567"),
], ids=["two-clients", "other-client", "unattributed-with-client", "partly-attributed", "null-client", "case"])
def test_rows_of_another_or_an_unknown_client_are_refused(rows, client_id):
    with pytest.raises(ValueError, match="client"):
        MappingTable.from_rows(rows, client_id=client_id)


def test_a_mapping_table_is_read_only_and_validated_however_it_is_built():
    t = table(("ledger", "a", "b"))
    with pytest.raises(TypeError):
        t.rows["ledger"]["x"] = "y"  # type: ignore[index]
    with pytest.raises(TypeError):
        t.rows["gst"] = {}  # type: ignore[index]
    for rows, error in (({"gst": {}}, ValueError), ({"ledger": {"a": " b"}}, ValueError),
                        ({"ledger": [("a", "b")]}, TypeError), ([("ledger", {})], TypeError)):
        with pytest.raises(error):
            MappingTable(rows=rows)  # type: ignore[arg-type]
    assert copy.deepcopy(t) == t == pickle.loads(pickle.dumps(t)) and MappingTable().get("ledger", "a") is None


# ---------- unsupported kinds and the outcome mapping ----------


@pytest.mark.parametrize("kind", ["tax", "tds", "cost_centre"])
def test_tax_tds_and_cost_centre_mappings_are_kept_but_refused_on_use(kind):
    t = table((kind, "CGST 9%", "Input CGST"))
    assert t.rows[kind] == {"CGST 9%": "Input CGST"}
    with pytest.raises(ValueError, match="unsupported"):
        t.get(kind, "CGST 9%")
    masters = LedgerMasters(CO.guid, frozenset({"Input CGST"}))
    r = resolve_ledger("CGST 9%", masters, CO.guid, t)  # a line named like the key is never translated by it
    assert r.status is LedgerStatus.MISSING and r.tally_name is None


@pytest.mark.parametrize("kind", ["legder", "Ledger", "", None])
def test_an_unknown_kind_is_refused_on_use_never_read_as_no_mapping(kind):
    with pytest.raises(ValueError, match="not one of"):
        table(("ledger", "a", "b")).get(kind, "a")


def test_every_status_has_a_problem_code_and_no_status_was_added():
    assert {s.value for s in LedgerStatus} == {"exists", "missing", "ambiguous", "masters_unavailable",
                                                "company_unverified"}
    assert set(voucher_xml._RESOLUTION_CODES) == set(LedgerStatus) - {LedgerStatus.EXISTS}


# ---------- resolution: mapping vs direct name ----------


@pytest.mark.parametrize("name,rows,status,tally_name", [
    (PURCHASE, (), LedgerStatus.EXISTS, PURCHASE),
    ("Purchase - RM", (("ledger", "Purchase - RM", PURCHASE),), LedgerStatus.EXISTS, PURCHASE),
    (PURCHASE, (("ledger", PURCHASE, PURCHASE),), LedgerStatus.EXISTS, PURCHASE),
    (PURCHASE, (("ledger", PURCHASE, "Ghost Ledger"),), LedgerStatus.MISSING, None),
    ("RM", (("ledger", "RM", "Steel"), ("ledger", "Steel", PURCHASE)), LedgerStatus.MISSING, None),
    ("Purchase - RM", (("ledger", "Purchase - RM", PURCHASE.upper()),), LedgerStatus.AMBIGUOUS, None),
    ("Purchase - RM", (("ledger", "purchase - rm", PURCHASE),), LedgerStatus.AMBIGUOUS, None),
    ("Purchase - RM", (("ledger", "Purchase  -  RM", PURCHASE),), LedgerStatus.AMBIGUOUS, None),
    ("Purchase - RM", (("ledger", "Purchase - RM", PURCHASE), ("ledger", "purchase - rm", PURCHASE)),
     LedgerStatus.EXISTS, PURCHASE),
    ("Purchase - RM", (("ledger", "Purchase - RM", PURCHASE), ("ledger", "purchase - rm", "Cash")),
     LedgerStatus.AMBIGUOUS, None),
    ("Steel Purchase", (), LedgerStatus.MISSING, None),
    ("finledger test purchase", (), LedgerStatus.AMBIGUOUS, None),
], ids=["direct", "mapped", "mapped-to-itself", "mapping-is-authoritative", "never-chained", "mapped-to-case-variant",
        "key-differs-by-case", "key-differs-by-spacing", "exact-key-wins", "exact-key-with-conflicting-twin", "missing",
        "case-variant"])
def test_ledger_resolution_via_mapping_or_direct_name(name, rows, status, tally_name):
    r = resolve_ledger(name, ALIASED, CO.guid, table(*rows) if rows else None)
    assert (r.status, r.tally_name) == (status, tally_name), r.detail


@pytest.mark.parametrize("party,rows,gstin,status", [
    (VENDOR, (("party", GSTIN, VENDOR),), GSTIN, LedgerStatus.EXISTS),
    (VENDOR, (("party", GSTIN, "Cash"),), GSTIN, LedgerStatus.AMBIGUOUS),
    ("Cash", (("party", GSTIN, VENDOR),), GSTIN, LedgerStatus.AMBIGUOUS),
    (VENDOR, (("party", GSTIN, VENDOR),), GSTIN.lower(), LedgerStatus.AMBIGUOUS),
    (VENDOR, (("party", GSTIN, "Cash"),), None, LedgerStatus.EXISTS),
    (VENDOR, (("party", "29AAACB1234C1Z5", "Cash"),), GSTIN, LedgerStatus.EXISTS),
    (VENDOR, (("ledger", VENDOR, "Cash"),), None, LedgerStatus.EXISTS),
    ("Brand New Vendor", (("party", GSTIN, "Brand New Vendor"),), GSTIN, LedgerStatus.MISSING),
], ids=["gstin-agrees", "gstin-disagrees", "gstin-replaces-nothing", "gstin-differs-by-case", "no-gstin",
        "gstin-without-row", "ledger-kind-not-for-party", "agreeing-but-missing"])
def test_party_resolution_cross_checks_but_never_replaces_the_drafts_name(party, rows, gstin, status):
    r = resolve_party(party, REAL_MASTERS, CO.guid, table(*rows), gstin)
    assert r.status is status, r.detail
    assert r.tally_name == (party if status is LedgerStatus.EXISTS else None)


@pytest.mark.parametrize("call", [
    lambda: resolve_ledger(None, REAL_MASTERS, CO.guid),
    lambda: resolve_ledger(b"Cash", REAL_MASTERS, CO.guid),
    lambda: resolve_party(5, REAL_MASTERS, CO.guid),
    lambda: resolve_party(VENDOR, REAL_MASTERS, CO.guid, table(("party", GSTIN, VENDOR)), 27),
])
def test_non_text_names_raise_instead_of_resolving(call):
    with pytest.raises(TypeError):
        call()


# ---------- nothing is ever created, altered or imported because of a mapping ----------


def _book_with_alias() -> list[str]:
    return [row(VENDOR, "Sundry Creditors", 0xce).decode(),
            row(PURCHASE, "Purchase Accounts", 0xcf, [PURCHASE, "Purchases"]).decode(),
            row("Cash", "Cash-in-Hand", 0x1f).decode()]


@pytest.mark.parametrize("target,code", [
    ("Purchases", "ambiguous_ledger"), ("Steel Purchase", "missing_ledger"), (PURCHASE.lower(), "ambiguous_ledger"),
], ids=["alias", "missing", "case-variant"])
def test_a_bad_mapping_target_never_reaches_an_import(served, tmp_path, target, code):
    fake = served(ledgers={CO.guid: _book_with_alias()})
    j = make_job(lines=[{"ledger": "Purchase - RM", "amount": "1180.00", "is_debit": True, "cost_centre": None}])
    mapping = table(("ledger", "Purchase - RM", target))
    out = post_approved(j, approval(j), settings_for(fake, tmp_path), mapping=mapping)
    assert out.state == State.VALIDATION_REJECTED and code in dict(out.problems)
    assert fake.imports == [] and "import" not in fake.kinds and not out.sent


def test_look_alike_ledgers_in_tally_never_reach_an_import(served, tmp_path):
    fake = served(ledgers={CO.guid: _book_with_alias() + [row("FinLedger\u00a0Test Purchase", "Purchase Accounts",
                                                                   0x40).decode()]})
    j = make_job()
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.VALIDATION_REJECTED and "ambiguous_ledger" in dict(out.problems)
    assert fake.imports == [] and "import" not in fake.kinds


def test_a_mapping_for_another_client_is_refused_before_anything_runs():
    with pytest.raises(ValueError, match="client"):
        MappingTable.from_rows([{**_r(CLIENT), "client_id": "44444444-4444-4444-8444-444444444444"}], client_id=CLIENT)


def test_resolution_never_changes_the_masters_and_the_modules_cannot_write_to_tally():
    before = copy.deepcopy(ALIASED)
    for name in (PURCHASE, "Purchases", "Ghost", PURCHASE.lower()):
        resolve_ledger(name, ALIASED, CO.guid, table(("ledger", "Ghost", "Purchases")))
    assert ALIASED == before
    for module in ("mapping.py", "masters.py"):
        src = (REPO / "person4_tally/src/finledger_tally" / module).read_text()
        assert not re.search(r"Import Data|ACTION=|TALLYMESSAGE|\.import_|post_approved", src), module
