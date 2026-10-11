"""Round-2 regressions for the ledger masters parser and ledger mapping (reviewer and audit findings, 2026-10-11).

Every adversarial reply is the REAL TallyPrime EDU 7.1 ledger reply
(fixtures/tallyprime_7_1_edu_masters_20261011_013427) with one targeted change. Nothing here talks to a Tally:
parsing and resolution are pure functions.
"""

from __future__ import annotations

import copy
import pathlib
import pickle

import pytest

from finledger_tally import LedgerStatus, MappingTable, TallyMalformedResponse, parse_ledger_export
from finledger_tally import resolve_ledger, resolve_party
from finledger_tally.masters import _refuse_raw_breaks_in_attributes
from test_mapping_hardening import CO, GSTIN, LEDGERS, PURCHASE, REAL_MASTERS, VENDOR, edit

HERE = pathlib.Path(__file__).parent
GHOST = (b'<LEDGER NAME="Ghost" RESERVEDNAME=""><GUID TYPE="String">' + CO.guid.encode() + b'-00000099</GUID>'
         b'<PARENT TYPE="String">Sundry Creditors</PARENT></LEDGER>')


def test_real_reply_still_parses_with_its_cmpinfo_ledger_counter():
    assert b"<CMPINFO>" in LEDGERS and b"    <LEDGER>0</LEDGER>" in LEDGERS
    assert REAL_MASTERS.ledgers == {"Cash", PURCHASE, VENDOR, "Profit & Loss A/c"}


# --- Finding 1: only the CMPINFO counter leaf is exempt from the stray-LEDGER check --------------------------------

@pytest.mark.parametrize("raw", [
    edit(b"   </CMPINFO>\r\n", b"   </CMPINFO>\r\n" + GHOST),                     # a full row directly under DESC
    edit(b"  <DESC>\r\n", b"  <DESC>\r\n<X>" + GHOST + b"</X>"),                  # nested deeper in DESC
    edit(b"    <LEDGER>0</LEDGER>\r\n", b"    " + GHOST + b"\r\n"),             # a full row in place of the counter
    edit(b"    <LEDGER>0</LEDGER>\r\n", b"    <LEDGER>0<GUID/></LEDGER>\r\n"),  # the counter grown a child
    edit(b"   </CMPINFO>\r\n", b"   </CMPINFO>\r\n<LEDGER>0</LEDGER>"),          # a counter-like leaf outside CMPINFO
    edit(b"   </CMPINFO>\r\n", b"   </CMPINFO>\r\n<ledger>0</ledger>"),          # lower case, in DESC
    edit(b"   </CMPINFO>\r\n", b"   </CMPINFO>\r\n<Ledger NAME='Ghost'/>"),     # mixed case, in DESC
    edit(b"   </COLLECTION>", b"   </COLLECTION><ledger/>"),                       # lower case, in DATA
], ids=["row-under-desc", "row-nested-in-desc", "row-as-cmpinfo-counter", "counter-with-child",
        "leaf-outside-cmpinfo", "lowercase-in-desc", "mixedcase-in-desc", "lowercase-in-data"])
def test_ledger_elements_outside_the_collection_refuse_the_reply(raw):
    with pytest.raises(TallyMalformedResponse, match="LEDGER element"):
        parse_ledger_export(raw, CO)


# --- Finding 2: name / parent / GUID fields with mixed content are refused, not truncated --------------------------

@pytest.mark.parametrize("raw", [
    edit(b">Cash-in-Hand</PARENT>", b">Cash<X/>-in-Hand</PARENT>"),
    edit(b">Cash-in-Hand</PARENT>", b">Cash-in-Hand<X/></PARENT>"),
    edit(b"-0000001f</GUID>", b"-0000001f<X/>tail</GUID>"),
    edit(b"<NAME>Cash</NAME>", b"<NAME>Cash<X/></NAME>"),
    edit(b"<NAME>Cash</NAME>", b"<NAME>Cash<X/>Book</NAME>"),
], ids=["parent-child-and-tail", "parent-trailing-child", "guid-tail", "namelist-child", "namelist-child-and-tail"])
def test_mixed_content_in_ledger_fields_is_refused(raw):
    with pytest.raises(TallyMalformedResponse, match="mixed content"):
        parse_ledger_export(raw, CO)


def test_masters_field_reading_does_not_depend_on_identity_text(monkeypatch):
    from finledger_tally import identity
    from finledger_tally import masters

    def lax(el, tag):  # reads .text only: what a laxer identity._text would do
        return None if el.find(tag) is None else el.find(tag).text
    for module in (identity, masters):  # masters must not read its fields through identity._text at all
        if hasattr(module, "_text"):
            monkeypatch.setattr(module, "_text", lax)
    with pytest.raises(TallyMalformedResponse, match="mixed content"):
        parse_ledger_export(edit(b">Cash-in-Hand</PARENT>", b">Cash<X/>-in-Hand</PARENT>"), CO)


# --- Finding 3: an exact mapping key never wins over a look-alike key that maps to a different ledger --------------

@pytest.mark.parametrize("twin", ["purchase - rm", "Purchase  - RM", "Purchase\u00a0- RM", "Purchase - RM\u200b",
                                  "\uff30urchase - RM"],
                         ids=["case", "spacing", "nbsp", "zero-width", "full-width"])
def test_exact_mapping_key_with_conflicting_look_alike_is_ambiguous(twin):
    table = MappingTable.from_rows([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": PURCHASE},
                                    {"kind": "ledger", "our_key": twin, "erp_value": "Cash"}])
    got = resolve_ledger("Purchase - RM", REAL_MASTERS, CO.guid, table)
    assert (got.status, got.tally_name) == (LedgerStatus.AMBIGUOUS, None)
    assert "neither is chosen" in got.detail


def test_exact_mapping_key_with_agreeing_look_alike_still_resolves():
    table = MappingTable.from_rows([{"kind": "ledger", "our_key": "Purchase - RM", "erp_value": PURCHASE},
                                    {"kind": "ledger", "our_key": "purchase - rm", "erp_value": PURCHASE}])
    got = resolve_ledger("Purchase - RM", REAL_MASTERS, CO.guid, table)
    assert (got.status, got.tally_name) == (LedgerStatus.EXISTS, PURCHASE)


def test_party_gstin_with_conflicting_look_alike_key_is_ambiguous():
    table = MappingTable.from_rows([{"kind": "party", "our_key": GSTIN, "erp_value": VENDOR},
                                    {"kind": "party", "our_key": GSTIN.lower(), "erp_value": "Cash"}])
    got = resolve_party(VENDOR, REAL_MASTERS, CO.guid, table, vendor_gstin=GSTIN)
    assert (got.status, got.tally_name) == (LedgerStatus.AMBIGUOUS, None)


# --- Finding 4: comments, CDATA and PIs cannot hide a start tag from the raw-break scan ----------------------------

@pytest.mark.parametrize("hider", ["<!-- <X Y=' -->", "<![CDATA[ <X Y=' ]]>", "<?pi <X Y=' ?>", '<!-- <X Y=" -->'],
                         ids=["comment", "cdata", "pi", "comment-double-quote"])
def test_raw_break_scan_is_not_fooled_by_unbalanced_quotes(hider):
    text = f'<ENVELOPE>{hider}<LEDGER Z=\'>\' NAME="Ca\nsh"/></ENVELOPE>'
    with pytest.raises(TallyMalformedResponse, match="raw tab or line break"):
        _refuse_raw_breaks_in_attributes(text)


def test_raw_break_scan_accepts_quotes_in_skipped_markup_without_breaks():
    _refuse_raw_breaks_in_attributes("<A><!-- it's --><B X='1'/><![CDATA[ \" ]]><?pi ' ?></A>")


@pytest.mark.parametrize("markup", [b"<!-- c -->", b"<![CDATA[x]]>", b"<?pi x?>"], ids=["comment", "cdata", "pi"])
def test_ledger_reply_with_comment_cdata_or_pi_is_refused(markup):
    with pytest.raises(TallyMalformedResponse, match="comment, CDATA section or processing instruction"):
        parse_ledger_export(edit(b">Cash-in-Hand</PARENT>", b">Cash-in" + markup + b"-Hand</PARENT>"), CO)


def test_ledger_reply_with_an_xml_declaration_still_parses():
    got = parse_ledger_export(b'<?xml version="1.0" encoding="utf-8"?>\r\n' + LEDGERS, CO)
    assert got.ledgers == REAL_MASTERS.ledgers


def test_real_fixtures_hold_no_comment_cdata_or_pi():
    for path in (HERE / "fixtures").glob("tallyprime_*/response_*.xml"):
        data = path.read_bytes()
        assert b"<!--" not in data and b"<![CDATA[" not in data and b"<?" not in data, path


# --- Finding 5: the mapping test sources are plain ASCII (invisible / bidi characters only as escapes) ----------

@pytest.mark.parametrize("name", ["test_mapping_hardening.py", "test_masters_round2.py"])
def test_test_sources_are_ascii(name):
    text = (HERE / name).read_text(encoding="utf-8")
    assert [hex(ord(c)) for c in sorted(set(text)) if ord(c) > 126] == []


# --- Finding 6: a MappingTable records the client its rows were checked against ------------------------------------

def _rows(client: object = None) -> list[dict]:
    row = {"kind": "ledger", "our_key": "Rent", "erp_value": "Cash"}
    return [row if client is None else {**row, "client_id": client}]


def test_mapping_table_records_the_checked_client():
    with pytest.raises(ValueError, match="carry no client_id"):
        MappingTable.from_rows(_rows(), client_id="c-1")
    table = MappingTable.from_rows(_rows("c-1"), client_id="c-1")
    assert table.client_id == "c-1" and table.belongs_to("c-1") and not table.belongs_to("c-2")
    inferred = MappingTable.from_rows(_rows(7))
    assert inferred.client_id == "7" and inferred.belongs_to(7) and inferred.belongs_to("7")
    with pytest.raises(ValueError, match="same client"):
        MappingTable.from_rows(_rows("c-1"), client_id="c-2")


def test_mapping_table_without_a_client_never_vouches():
    for table in (MappingTable.from_rows(_rows()), MappingTable({"ledger": {"Rent": "Cash"}}), MappingTable()):
        assert table.client_id is None
        assert not table.belongs_to("c-1") and not table.belongs_to(None) and not table.belongs_to("")


def test_mapping_table_client_survives_copy_and_pickle():
    table = MappingTable.from_rows(_rows("c-1"))
    for twin in (copy.copy(table), copy.deepcopy(table), pickle.loads(pickle.dumps(table))):
        assert twin == table and twin.client_id == "c-1" and twin.get("ledger", "Rent") == "Cash"


@pytest.mark.parametrize("bad", ["", " ", " c-1", 7], ids=["empty", "blank", "padded", "not-text"])
def test_mapping_table_refuses_a_bad_client_id(bad):
    with pytest.raises(ValueError, match="client_id"):
        MappingTable({"ledger": {"Rent": "Cash"}}, client_id=bad)
