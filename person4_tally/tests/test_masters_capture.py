"""Run-Masters-Capture.ps1 (the one-click, read-only ledger capture) against a SYNTHETIC local Tally.

The company list reply is the real capture's shape; the Ledger replies are the same synthetic shape test_masters.py
uses. These tests prove the script's requests, checks and verdicts, not how TallyPrime answers. Needs PowerShell.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from finledger_tally import TallyCompany, parse_company_list, parse_ledger_export, resolve_company
from finledger_tally.company import xml_text
from finledger_tally.masters import ledger_request
from test_identity_probe_kit import KIT, PWSH, _FakeTally
from test_isolation_check import OTHER_CO, REJECTED, TEST_CO
from test_masters import BOOK, OTHER_BOOK, Tally, ledger, ledgers_reply

FILES = ("capture.ps1", "company_list_request.xml", "ledger_export_request.xml", "Run-Masters-Capture.ps1")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell not available")


def run(tmp_path: Path, model, company: str = TEST_CO[0]):
    for name in FILES:
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"", responder=model)
    try:
        result = subprocess.run(
            [PWSH, "-NoProfile", "-File", str(tmp_path / "Run-Masters-Capture.ps1"), "-Company", company,
             "-Port", str(fake.port), "-NoExplorer"], capture_output=True, text=True, timeout=180)
    finally:
        fake.close()
    runs = sorted((tmp_path / "captures").glob("masters_*/"))
    summary = (runs[0] / "masters_summary.txt").read_text() if runs else ""
    return result, fake, (runs[0] if runs else None), summary


def _only_exports(fake):
    for req in fake.received:
        assert [e.text for e in ET.fromstring(req).iter("TALLYREQUEST")] == ["Export"]
        assert not re.search(rb"import|action\s*=|<voucher\b|IMPORTDATA|TALLYMESSAGE", req, re.IGNORECASE)


@pytest.mark.parametrize("name", ["FinLedger Test Company", "R&D <Ltd> \"Main\" 'Unit'"])
def test_python_ledger_request_equals_the_kit_template(name):
    template = (KIT / "ledger_export_request.xml").read_bytes().replace(b"\r\n", b"\n")
    filled = template.replace(b"__COMPANY__", xml_text(name).encode("utf-8"))
    assert ledger_request(TallyCompany(name, TEST_CO[1])) == filled


@needs_pwsh
def test_clean_capture_sends_three_exports_pinned_to_the_resolved_company(tmp_path):
    model = Tally(companies=[TEST_CO, OTHER_CO])
    result, fake, run_dir, summary = run(tmp_path, model)
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary.startswith("verdict: CAPTURED_CLEAN")
    assert len(fake.received) == 3
    _only_exports(fake)
    assert ET.fromstring(fake.received[1]).findtext(".//SVCURRENTCOMPANY") == TEST_CO[0]
    assert (run_dir / "request_02_ledgers.xml").read_bytes() == fake.received[1]
    assert (run_dir / "response_02_ledgers.xml").read_bytes() == fake.sent[1]
    assert f"company_guid: {TEST_CO[1]}" in summary and "ledger_rows: 4 own_company_guid: 4" in summary
    assert "ledger_reply_has_char_ref_4: True" in summary
    # The Python parser accepts the very bytes the capture saved.
    company = resolve_company(parse_company_list((run_dir / "response_01_companies.xml").read_bytes()), TEST_CO[0])
    masters = parse_ledger_export((run_dir / "response_02_ledgers.xml").read_bytes(), company)
    assert "FinLedger Test Vendor" in masters.ledgers and len(masters.ledgers) == 4
    assert list((tmp_path / "captures").glob("FinLedger_Masters_*.zip"))


@needs_pwsh
@pytest.mark.parametrize("rows,difference", [
    (BOOK + OTHER_BOOK, "carry another company's GUID"),
    (BOOK + [ledger("Odd", "Sundry Creditors", guid="00000001")], "not <company GUID>-<8 hex>"),
    (BOOK + [ledger("cash", "Cash-in-Hand", n=0x99)], "name/parent problems"),
    (BOOK + [ledger("No Parent", "", n=0x98)], "name/parent problems"),
    ([], "no LEDGER rows"),
])
def test_differences_are_captured_not_hidden(tmp_path, rows, difference):
    result, fake, run_dir, summary = run(tmp_path, Tally(books={TEST_CO[1]: rows}))
    assert result.returncode == 3, result.stdout + result.stderr
    assert summary.startswith("verdict: CAPTURED_WITH_DIFFERENCES") and difference in summary
    assert len(fake.received) == 3 and (run_dir / "response_02_ledgers.xml").exists()


@needs_pwsh
def test_a_rejected_ledger_reply_is_a_difference(tmp_path):
    result, _, _, summary = run(tmp_path, Tally(books={TEST_CO[1]: REJECTED}))
    assert result.returncode == 3 and "ledger reply rejected" in summary


@needs_pwsh
@pytest.mark.parametrize("companies,company,why", [
    ([OTHER_CO], TEST_CO[0], "is not loaded"),
    ([TEST_CO], "finledger test company", "is not loaded"),
    ([TEST_CO, (TEST_CO[0], OTHER_CO[1])], TEST_CO[0], "cannot be determined"),
    ([TEST_CO, (OTHER_CO[0], TEST_CO[1])], TEST_CO[0], "same GUID"),
])
def test_stops_before_any_ledger_request_when_the_company_is_not_certain(tmp_path, companies, company, why):
    result, fake, _, summary = run(tmp_path, Tally(companies=companies), company=company)
    assert result.returncode == 1
    assert summary.startswith("verdict: STOPPED") and why in summary
    assert len(fake.received) == 1  # never a pin to a company that is not loaded (crashed TallyPrime 7.1 EDU)


@needs_pwsh
def test_stops_when_the_company_guid_changes_during_the_capture(tmp_path):
    swapped = [(TEST_CO[0], OTHER_CO[1])]
    result, fake, _, summary = run(tmp_path, Tally(companies=[TEST_CO], later_companies=swapped))
    assert result.returncode == 1 and "changed GUID" in summary
    assert len(fake.received) == 3


@needs_pwsh
def test_stops_when_tally_is_closed(tmp_path):
    for name in FILES:
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"")
    port = fake.port
    fake.close()
    result = subprocess.run([PWSH, "-NoProfile", "-File", str(tmp_path / "Run-Masters-Capture.ps1"),
                             "-Port", str(port), "-NoExplorer"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 1 and "STOPPED: TallyPrime is not answering" in result.stdout


def test_scripts_send_nothing_themselves_and_are_ascii():
    src = (KIT / "Run-Masters-Capture.ps1").read_text(encoding="ascii")
    assert not re.search(r"Invoke-WebRequest|Invoke-RestMethod|WebClient|HttpClient", src)
    assert not re.search(r"https?://", src)
    assert "DtdProcessing]::Prohibit" in src and "'127.0.0.1'" in src
    launcher = (KIT / "Run-Masters-Capture.cmd").read_bytes()
    launcher.decode("ascii")
    assert b"\r\n" in launcher and b"Run-Masters-Capture.ps1" in launcher
    (KIT / "ledger_export_request.xml").read_text(encoding="ascii")
