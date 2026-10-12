"""Run-Isolation-Check.ps1 against a fake local Tally that models several loaded companies.

The fake's replies reuse the REAL capture's envelope and voucher block, but every multi-company behaviour here
(how Tally treats SVCURRENTCOMPANY) is SYNTHETIC: these tests prove the probe's
checks and fail-closed paths, not how TallyPrime behaves. Needs PowerShell (pwsh on PATH or FINLEDGER_PWSH).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from finledger_tally import LookupStatus, find_by_marker, parse_company_list, parse_voucher_export, resolve_company
from test_identity_probe_kit import KIT, PWSH, _FakeTally

REAL = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_run_20261010_162316"
DOC = "9d4f3d5d-499e-41db-b510-e98348e5b503"
TEST_CO = ("FinLedger Test Company", "7a5f42d4-ec1d-4aab-9992-e2e217bcb773")
OTHER_CO = ("Other Existing Company", "0b1c2d3e-4f50-4a61-8b72-c83d94e5f601")  # synthetic
FILES = ("capture.ps1", "voucher_export_request.xml", "company_list_request.xml", "Run-Isolation-Check.ps1")

pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell not available")

_VOUCHERS_REPLY = (REAL / "response_02_day_before_edit.xml").read_text(encoding="utf-8")
_V_START = _VOUCHERS_REPLY.index("    <VOUCHER REMOTEID")
_V_END = _VOUCHERS_REPLY.index("    </VOUCHER>") + len("    </VOUCHER>\r\n")
_VOUCHER_BLOCK = _VOUCHERS_REPLY[_V_START:_V_END]
_COMPANIES_REPLY = (REAL / "response_01_companies.xml").read_text(encoding="utf-8")
_C_START = _COMPANIES_REPLY.index("    <COMPANY NAME")
_C_END = _COMPANIES_REPLY.index("    </COMPANY>", _C_START) + len("    </COMPANY>\r\n")
_COMPANY_BLOCK = _COMPANIES_REPLY[_C_START:_C_END]


def company_list(companies: list[tuple[str, str]]) -> bytes:
    blocks = "".join(_COMPANY_BLOCK.replace(TEST_CO[0], name).replace(TEST_CO[1], guid) for name, guid in companies)
    return (_COMPANIES_REPLY[:_C_START] + blocks + _COMPANIES_REPLY[_C_END:]).encode()


def voucher(company_guid: str, master_id: int, doc: str | None = DOC, guid: str | None = None) -> str:
    block = _VOUCHER_BLOCK.replace(f"{TEST_CO[1]}-00000001", guid or f"{company_guid}-{master_id:08x}")
    block = block.replace(TEST_CO[1], company_guid).replace("> 1</MASTERID>", f"> {master_id}</MASTERID>")
    return block.replace(DOC, doc) if doc else block.replace(f" finledger:document_id={DOC}", "")


def vouchers_reply(blocks: list[str]) -> bytes:
    return (_VOUCHERS_REPLY[:_V_START] + "".join(blocks) + _VOUCHERS_REPLY[_V_END:]).encode()


REJECTED = (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA>"
            b"<LINEERROR>Could not set &apos;SVCurrentCompany&apos;</LINEERROR></DATA></BODY></ENVELOPE>")


class Model:
    """Synthetic Tally: companies loaded, vouchers per company GUID, and how a pinned export behaves."""

    def __init__(self, companies, books, pin="honour", active=TEST_CO[1]):
        self.companies, self.books, self.pin, self.active = companies, books, pin, active

    def __call__(self, body: bytes, n: int) -> bytes:
        text = body.decode()
        if "<TYPE>Company</TYPE>" in text:
            return company_list(self.companies)
        name = ET.fromstring(text).findtext(".//SVCURRENTCOMPANY")
        guid = next((g for nm, g in self.companies if nm == name), None)
        if self.pin == "ignore":
            return vouchers_reply([b for blocks in self.books.values() for b in blocks])
        if self.pin == "active":
            return vouchers_reply(self.books.get(self.active, []))
        if guid is None:
            return REJECTED
        return vouchers_reply(self.books.get(guid, []))


def run(tmp_path: Path, model: Model, company: str = TEST_CO[0]):
    for name in FILES:
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"", responder=model)
    try:
        result = subprocess.run(
            [PWSH, "-NoProfile", "-File", str(tmp_path / "Run-Isolation-Check.ps1"), "-Company", company,
             "-Port", str(fake.port), "-NoExplorer"], capture_output=True, text=True, timeout=180)
    finally:
        fake.close()
    runs = sorted((tmp_path / "captures").glob("isolation_*/"))
    summary = (runs[0] / "isolation_summary.txt").read_text() if runs else ""
    return result, fake, (runs[0] if runs else None), summary


TWO = [TEST_CO, OTHER_CO]
BOTH_BOOKS = {TEST_CO[1]: [voucher(TEST_CO[1], 1)], OTHER_CO[1]: [voucher(OTHER_CO[1], 1, doc=None)]}


def test_pass_when_each_pin_returns_only_its_own_company(tmp_path):
    result, fake, run_dir, summary = run(tmp_path, Model(TWO, BOTH_BOOKS))
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary.startswith("verdict: PASS")
    # 1 company list + one pinned export per loaded company, all Export. Never a pin to a company that is not loaded:
    # the real TallyPrime 7.1 EDU crashed (c0000005) on that request on 2026-10-10.
    assert len(fake.received) == 3
    for req in fake.received:
        assert [e.text for e in ET.fromstring(req).iter("TALLYREQUEST")] == ["Export"]
        assert not re.search(rb"import|action\s*=|<voucher\b", req, re.IGNORECASE)
    pins = [ET.fromstring(r).findtext(".//SVCURRENTCOMPANY") for r in fake.received[1:]]
    assert pins == [TEST_CO[0], OTHER_CO[0]]
    # The Python lookup agrees with the probe on the saved bytes.
    company = resolve_company(parse_company_list((run_dir / "response_01_companies.xml").read_bytes()), TEST_CO[0])
    own = parse_voucher_export((run_dir / "response_02_vouchers_pinned_company1.xml").read_bytes())
    assert find_by_marker(own, DOC, company.guid).status is LookupStatus.FOUND
    assert (run_dir / "response_02_vouchers_pinned_company1.xml").read_bytes() == fake.sent[1]


def test_partial_pass_when_the_other_company_has_no_vouchers(tmp_path):
    result, _, _, summary = run(tmp_path, Model(TWO, {TEST_CO[1]: [voucher(TEST_CO[1], 1)]}))
    assert result.returncode == 3
    assert summary.startswith("verdict: PARTIAL_PASS")


@pytest.mark.parametrize("pin", ["ignore", "active"])
def test_not_proven_when_a_pin_returns_another_companys_vouchers(tmp_path, pin):
    result, _, _, summary = run(tmp_path, Model(TWO, BOTH_BOOKS, pin=pin))
    assert result.returncode == 2
    assert summary.startswith("verdict: ISOLATION_NOT_PROVEN")
    assert "foreign=1" in summary


def test_not_proven_when_a_pinned_reply_has_a_malformed_guid(tmp_path):
    books = {TEST_CO[1]: [voucher(TEST_CO[1], 1)], OTHER_CO[1]: [voucher(OTHER_CO[1], 1, doc=None, guid="00000001")]}
    result, _, _, summary = run(tmp_path, Model(TWO, books))
    assert result.returncode == 2 and "malformed=1" in summary


def test_inconclusive_when_the_marker_is_not_in_the_test_company(tmp_path):
    books = {TEST_CO[1]: [voucher(TEST_CO[1], 1, doc=None)], OTHER_CO[1]: [voucher(OTHER_CO[1], 1, doc=None)]}
    result, _, _, summary = run(tmp_path, Model(TWO, books))
    assert result.returncode == 3 and summary.startswith("verdict: INCONCLUSIVE")


@pytest.mark.parametrize("companies,company,why", [
    ([TEST_CO], TEST_CO[0], "Only one company is loaded"),
    ([OTHER_CO, ("Third Co", "1b1c2d3e-4f50-4a61-8b72-c83d94e5f601")], TEST_CO[0], "is not loaded"),
    (TWO, "finledger test company", "is not loaded"),
    ([TEST_CO, (TEST_CO[0], OTHER_CO[1])], TEST_CO[0], "cannot be determined"),
    ([TEST_CO, (OTHER_CO[0], TEST_CO[1])], TEST_CO[0], "same GUID"),
    ([TEST_CO, (OTHER_CO[0], "not-a-guid")], TEST_CO[0], "8-4-4-4-12"),
])
def test_stops_before_any_voucher_request_when_the_company_is_not_certain(tmp_path, companies, company, why):
    result, fake, _, summary = run(tmp_path, Model(companies, BOTH_BOOKS), company=company)
    assert result.returncode == 1
    assert summary.startswith("verdict: STOPPED") and why in summary
    assert len(fake.received) == 1  # only the company list was requested


def test_stops_when_the_company_list_is_rejected(tmp_path):
    class Rejecting(Model):
        def __call__(self, body, n):
            return REJECTED
    result, fake, _, summary = run(tmp_path, Rejecting(TWO, BOTH_BOOKS))
    assert result.returncode == 1 and "company list rejected" in summary
    assert len(fake.received) == 1


def test_stops_when_tally_is_closed(tmp_path):
    for name in FILES:
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"")
    port = fake.port
    fake.close()
    result = subprocess.run([PWSH, "-NoProfile", "-File", str(tmp_path / "Run-Isolation-Check.ps1"),
                             "-Port", str(port), "-NoExplorer"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 1 and "STOPPED: TallyPrime is not answering" in result.stdout


def test_script_sends_nothing_itself_and_is_ascii():
    src = (KIT / "Run-Isolation-Check.ps1").read_text(encoding="ascii")
    assert not re.search(r"Invoke-WebRequest|Invoke-RestMethod|WebClient|HttpClient", src)
    assert not re.search(r"https?://", src)
    assert "DtdProcessing]::Prohibit" in src
