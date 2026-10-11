"""Identity lookup tests. The first block runs on the REAL TallyPrime EDU 7.1 capture; the rest mutates those real
bytes to cover cases the capture did not contain (those mutations are synthetic)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from finledger_tally import (
    CompanyNotResolved,
    LookupStatus,
    TallyMalformedResponse,
    TallyRequestRejected,
    belongs_to_company,
    find_by_marker,
    identity_marker,
    parse_company_list,
    parse_voucher_export,
    resolve_company,
    voucher_company_guid,
)

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
COMPANY_GUID = "7a5f42d4-ec1d-4aab-9992-e2e217bcb773"
VOUCHER_GUID = COMPANY_GUID + "-00000001"
OTHER_DOC = "11111111-1111-4111-8111-111111111111"


def real(name: str) -> bytes:
    return (REAL / f"response_{name}.xml").read_bytes()


def mutated(name: str, old: str, new: str, count: int = 1) -> bytes:
    raw = real(name)
    assert raw.count(old.encode()) >= 1, old
    return raw.replace(old.encode(), new.encode(), count)


# ---------- real capture ----------


def test_real_company_list():
    [company] = parse_company_list(real("01_companies"))
    assert company.name == "FinLedger Test Company"
    assert company.guid == COMPANY_GUID


def test_real_voucher_before_edit():
    [row] = parse_voucher_export(real("02_day_before_edit"))
    assert row.guid == VOUCHER_GUID
    assert row.master_id == 1 and row.alter_id == 3
    assert row.date == "20260401"
    assert row.voucher_type == "Purchase" and row.voucher_number == "1"
    assert row.party == "FinLedger Test Vendor" and row.reference == "TEST-INV-001"
    assert row.amount == Decimal("1180.00")
    assert row.narration == f"Test purchase finledger:document_id={DOC}"
    assert row.is_deleted is False


def test_real_edit_keeps_identity_and_bumps_alterid():
    [before] = parse_voucher_export(real("02_day_before_edit"))
    [after] = parse_voucher_export(real("05_day_after_edit"))
    assert (after.guid, after.master_id) == (before.guid, before.master_id)
    assert (before.alter_id, after.alter_id) == (3, 4)
    assert (before.amount, after.amount) == (Decimal("1180.00"), Decimal("2000.00"))
    unchanged = ("date", "voucher_type", "voucher_number", "party", "reference", "narration", "is_deleted")
    assert all(getattr(before, f) == getattr(after, f) for f in unchanged)


def test_real_date_window_was_not_applied():
    # Day, whole-year and an empty day all returned the same bytes: SVFROMDATE/SVTODATE did not filter.
    assert real("02_day_before_edit") == real("03_fy_before_edit") == real("04_other_day")
    assert real("05_day_after_edit") == real("06_fy_after_edit")


@pytest.mark.parametrize("name", ["02_day_before_edit", "03_fy_before_edit", "04_other_day",
                                  "05_day_after_edit", "06_fy_after_edit"])
def test_real_marker_lookup_finds_the_voucher(name):
    [company] = parse_company_list(real("01_companies"))
    result = find_by_marker(parse_voucher_export(real(name)), DOC, company.guid)
    assert result.status is LookupStatus.FOUND
    assert (result.row.guid, result.row.master_id) == (VOUCHER_GUID, 1)


def test_real_other_document_is_not_found():
    result = find_by_marker(parse_voucher_export(real("02_day_before_edit")), OTHER_DOC, COMPANY_GUID)
    assert result.status is LookupStatus.NOT_FOUND
    assert "does not prove" in result.reason


def test_real_voucher_belongs_to_its_company_only():
    [row] = parse_voucher_export(real("02_day_before_edit"))
    assert belongs_to_company(row, COMPANY_GUID)
    assert belongs_to_company(row, COMPANY_GUID.upper())
    assert not belongs_to_company(row, "7a5f42d4-ec1d-4aab-9992-e2e217bcb774")
    result = find_by_marker([row], DOC, "7a5f42d4-ec1d-4aab-9992-e2e217bcb774")
    assert result.status is LookupStatus.UNREADABLE


# ---------- synthetic mutations of the real reply ----------

OTHER_COMPANY = "0b1c2d3e-4f50-4a61-8b72-c83d94e5f601"  # synthetic: a second company never seen from real Tally


def _twin(company: str = COMPANY_GUID, guid_suffix: str = "00000002", master_id: int = 2,
          narration_doc: str = DOC):
    raw = mutated("02_day_before_edit", f"<GUID>{VOUCHER_GUID}</GUID>", f"<GUID>{company}-{guid_suffix}</GUID>")
    raw = raw.replace(b'<MASTERID TYPE="Number"> 1</MASTERID>', f'<MASTERID TYPE="Number"> {master_id}</MASTERID>'.encode())
    raw = raw.replace(DOC.encode(), narration_doc.encode())
    [row] = parse_voucher_export(raw)
    return row


def test_two_vouchers_with_the_marker_are_ambiguous():
    [row] = parse_voucher_export(real("02_day_before_edit"))
    twin = _twin(guid_suffix="00000002", master_id=2)
    result = find_by_marker([row, twin], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.AMBIGUOUS and result.row is None and len(result.rows) == 2


@pytest.mark.parametrize("narration", [
    f"Test purchase finledger:document_id={DOC.upper()}",
    f"Test purchase finledger:document_id={DOC[:-1]}",
    f"Test purchase finledger:document_id= {DOC}",
    f"Test purchase xfinledger:document_id={DOC}",
    f"Test purchase finledger:document_id={DOC}x",
    f"Test purchase FinLedger:Document_ID={DOC}",
    f"finledger:document_id={DOC} finledger:document_id={DOC}",
    f"finledger:document_id={DOC} finledger:document_id={OTHER_DOC}",
])
def test_damaged_or_doubled_markers_are_unreadable_not_missing(narration):
    raw = mutated("02_day_before_edit", f"Test purchase finledger:document_id={DOC}", narration)
    result = find_by_marker(parse_voucher_export(raw), DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE


def test_another_documents_exact_marker_is_simply_not_ours():
    raw = mutated("02_day_before_edit", DOC, OTHER_DOC)
    assert find_by_marker(parse_voucher_export(raw), DOC, COMPANY_GUID).status is LookupStatus.NOT_FOUND
    assert find_by_marker(parse_voucher_export(raw), OTHER_DOC, COMPANY_GUID).status is LookupStatus.FOUND


def test_missing_narration_element_is_unreadable():
    raw = mutated("02_day_before_edit", f'<NARRATION TYPE="String">Test purchase finledger:document_id={DOC}</NARRATION>', "")
    [row] = parse_voucher_export(raw)
    assert row.narration is None
    assert find_by_marker([row], DOC, COMPANY_GUID).status is LookupStatus.UNREADABLE


def test_deleted_voucher_is_not_found_as_live():
    raw = mutated("02_day_before_edit", "<ISDELETED>No</ISDELETED>", "<ISDELETED>Yes</ISDELETED>")
    assert find_by_marker(parse_voucher_export(raw), DOC, COMPANY_GUID).status is LookupStatus.UNREADABLE


def test_utf16_with_bom_parses_the_same():
    text = real("02_day_before_edit").decode("utf-8")
    assert parse_voucher_export(text.encode("utf-16")) == parse_voucher_export(real("02_day_before_edit"))


@pytest.mark.parametrize("old,new,error", [
    ("<STATUS>1</STATUS>", "<STATUS>0</STATUS>", TallyRequestRejected),
    ("<DATA>", "<DATA><LINEERROR>Could not find Company</LINEERROR>", TallyRequestRejected),
    ('<MASTERID TYPE="Number"> 1</MASTERID>', '<MASTERID TYPE="Number">x</MASTERID>', TallyMalformedResponse),
    ('<MASTERID TYPE="Number"> 1</MASTERID>', "", TallyMalformedResponse),
    (f"<GUID>{VOUCHER_GUID}</GUID>", "", TallyMalformedResponse),
    ("<VOUCHER REMOTEID", "<OBJECT REMOTEID", TallyMalformedResponse),
    ("</ENVELOPE>", "", TallyMalformedResponse),
])
def test_unexpected_shapes_fail_closed(old, new, error):
    raw = mutated("02_day_before_edit", old, new)
    if old == "<VOUCHER REMOTEID":
        raw = raw.replace(b"</VOUCHER>\r\n   </COLLECTION>", b"</OBJECT>\r\n   </COLLECTION>")
    with pytest.raises(error):
        parse_voucher_export(raw)


def test_voucher_reply_is_not_a_company_list():
    with pytest.raises(TallyMalformedResponse):
        parse_company_list(real("02_day_before_edit"))


@pytest.mark.parametrize("bad", [DOC.upper(), DOC[:-1], "not-a-uuid", "", None])
def test_identity_marker_refuses_non_canonical_ids(bad):
    with pytest.raises(ValueError):
        identity_marker(bad)


def test_identity_marker_matches_the_real_narration():
    [row] = parse_voucher_export(real("02_day_before_edit"))
    assert row.narration.endswith(identity_marker(DOC))


# ---------- company isolation ----------


def test_real_xml_company_identity_is_only_the_guid_prefix():
    # The company list names the company; the voucher row carries no company name, only the GUID prefix.
    [company] = parse_company_list(real("01_companies"))
    [row] = parse_voucher_export(real("02_day_before_edit"))
    assert voucher_company_guid(row) == company.guid == COMPANY_GUID
    assert b"FinLedger Test Company" not in real("02_day_before_edit")
    assert row.remote_id == VOUCHER_GUID
    assert row.vch_key.startswith(COMPANY_GUID + "-")


def test_resolve_company_requires_one_exact_name():
    companies = parse_company_list(real("01_companies"))
    assert resolve_company(companies, "FinLedger Test Company").guid == COMPANY_GUID
    for wrong in ("finledger test company", "FinLedger Test Company ", "FinLedger", "Other Co"):
        with pytest.raises(CompanyNotResolved):
            resolve_company(companies, wrong)
    with pytest.raises(CompanyNotResolved):
        resolve_company([], "FinLedger Test Company")


def test_two_loaded_companies_with_the_same_name_are_refused():
    raw = real("01_companies").decode()
    start = raw.index("<COMPANY NAME")
    block = raw[start:raw.index("</COMPANY>", start) + len("</COMPANY>")]
    raw = raw.replace(block, block + block.replace(COMPANY_GUID, OTHER_COMPANY))
    companies = parse_company_list(raw.encode())
    assert len(companies) == 2
    with pytest.raises(CompanyNotResolved):
        resolve_company(companies, "FinLedger Test Company")


@pytest.mark.parametrize("bad", ["", "   ", "7a5f42d4", "7a5f42d4-ec1d-4aab-9992-e2e217bcb77",
                                 "{7a5f42d4-ec1d-4aab-9992-e2e217bcb773}", VOUCHER_GUID, None])
def test_company_list_with_missing_or_malformed_guid_fails_closed(bad):
    if bad is None:
        raw = mutated("01_companies", f'<GUID TYPE="String">{COMPANY_GUID}</GUID>', "")
    else:
        raw = mutated("01_companies", f">{COMPANY_GUID}<", f">{bad}<")
    with pytest.raises(TallyMalformedResponse):
        parse_company_list(raw)


def test_company_list_with_duplicate_guid_fails_closed():
    raw = real("01_companies").decode()
    start = raw.index("<COMPANY NAME")
    block = raw[start:raw.index("</COMPANY>", start) + len("</COMPANY>")]
    raw = raw.replace(block, block + block.replace("FinLedger Test Company", "Copy Co"))
    with pytest.raises(TallyMalformedResponse):
        parse_company_list(raw.encode())


def test_marker_match_in_another_companys_voucher_is_rejected():
    foreign = _twin(company=OTHER_COMPANY, guid_suffix="00000001", master_id=1)
    result = find_by_marker([foreign], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE and result.row is None
    assert "not company-scoped" in result.reason
    assert find_by_marker([foreign], DOC, OTHER_COMPANY).status is LookupStatus.FOUND


def test_markers_in_two_companies_never_pick_one():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    foreign = _twin(company=OTHER_COMPANY, guid_suffix="00000001", master_id=1)
    for rows in ([ours, foreign], [foreign, ours]):
        for company in (COMPANY_GUID, OTHER_COMPANY):
            result = find_by_marker(rows, DOC, company)
            assert result.status is LookupStatus.UNREADABLE and result.row is None


def test_any_foreign_voucher_poisons_the_reply_even_without_its_marker():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    foreign = _twin(company=OTHER_COMPANY, guid_suffix="00000005", master_id=5, narration_doc=OTHER_DOC)
    result = find_by_marker([ours, foreign], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE
    assert find_by_marker([ours, foreign], "22222222-2222-4222-8222-222222222222", COMPANY_GUID).status \
        is LookupStatus.UNREADABLE


@pytest.mark.parametrize("guid", [
    COMPANY_GUID,                      # no suffix
    COMPANY_GUID + "-1",               # short suffix
    COMPANY_GUID + "-000000001",       # long suffix
    COMPANY_GUID + "-0000000g",        # not hex
    COMPANY_GUID + ":00000001",        # wrong separator
    "00000001",                        # no company part
    "7a5f42d4ec1d4aab9992e2e217bcb773-00000001",
])
def test_voucher_with_malformed_company_identity_is_unreadable(guid):
    raw = mutated("02_day_before_edit", f"<GUID>{VOUCHER_GUID}</GUID>", f"<GUID>{guid}</GUID>")
    [row] = parse_voucher_export(raw)
    assert voucher_company_guid(row) is None
    result = find_by_marker([row], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE and result.row is None


def test_upper_case_guid_from_tally_is_normalised_not_rejected():
    raw = mutated("02_day_before_edit", f"<GUID>{VOUCHER_GUID}</GUID>", f"<GUID>{VOUCHER_GUID.upper()}</GUID>")
    assert find_by_marker(parse_voucher_export(raw), DOC, COMPANY_GUID).status is LookupStatus.FOUND


@pytest.mark.parametrize("bad", ["", "  ", "FinLedger Test Company", "7a5f42d4", VOUCHER_GUID, None])
def test_lookup_refuses_a_malformed_requested_company(bad):
    rows = parse_voucher_export(real("02_day_before_edit"))
    with pytest.raises(ValueError):
        find_by_marker(rows, DOC, bad)


def test_duplicate_voucher_guid_is_unreadable_even_if_only_one_has_the_marker():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    copy = _twin(guid_suffix="00000001", master_id=7, narration_doc=OTHER_DOC)
    result = find_by_marker([ours, copy], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE and "guid" in result.reason


def test_duplicate_masterid_is_unreadable():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    copy = _twin(guid_suffix="00000009", master_id=1, narration_doc=OTHER_DOC)
    result = find_by_marker([ours, copy], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.UNREADABLE and "master_id" in result.reason


def test_exact_same_row_twice_is_unreadable_not_found():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    assert find_by_marker([ours, ours], DOC, COMPANY_GUID).status is LookupStatus.UNREADABLE


def test_match_is_accepted_whatever_its_date():
    # Dates are not trusted for scoping; a re-dated voucher must still be found, never silently skipped.
    raw = mutated("02_day_before_edit", '<DATE TYPE="Date">20260401</DATE>', '<DATE TYPE="Date">20250101</DATE>')
    result = find_by_marker(parse_voucher_export(raw), DOC, COMPANY_GUID)
    assert result.status is LookupStatus.FOUND and result.row.date == "20250101"


def test_distinct_vouchers_of_one_company_still_resolve():
    [ours] = parse_voucher_export(real("02_day_before_edit"))
    other = _twin(guid_suffix="00000002", master_id=2, narration_doc=OTHER_DOC)
    result = find_by_marker([other, ours], DOC, COMPANY_GUID)
    assert result.status is LookupStatus.FOUND and result.row.guid == VOUCHER_GUID
    assert find_by_marker([], DOC, COMPANY_GUID).status is LookupStatus.NOT_FOUND
