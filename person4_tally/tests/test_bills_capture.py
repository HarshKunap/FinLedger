"""Run-Bills-Capture.ps1 (optional, read-only) against a SYNTHETIC local Tally. The Bill reply shape is NOT real
evidence: no real Bill reply has been captured. These tests prove the script's requests, checks and verdicts only."""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fake_tally import bill
from finledger_tally import TallyCompany
from finledger_tally.bills import bill_request
from finledger_tally.company import xml_text
from test_identity_probe_kit import KIT, PWSH, _FakeTally
from test_isolation_check import OTHER_CO, TEST_CO, company_list
from test_masters import ledgers_reply

FILES = ("capture.ps1", "company_list_request.xml", "bill_export_request.xml", "Run-Bills-Capture.ps1")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell not available")
GOOD = [bill("TEST-INV-001", "FinLedger Test Vendor", "1180.00", "1180.00"),
        bill("OLD-1", "FinLedger Test Vendor", "-50.00", "0")]


class Model:
    def __init__(self, companies=(TEST_CO,), rows=GOOD):
        self.companies, self.rows = list(companies), rows

    def __call__(self, body, n):
        if b"<TYPE>Company</TYPE>" in body:
            return company_list(self.companies)
        return ledgers_reply(self.rows)


def run(tmp_path: Path, model, company: str = TEST_CO[0]):
    for name in FILES:
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"", responder=model)
    try:
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(tmp_path / "Run-Bills-Capture.ps1"), "-Company",
                                 company, "-Port", str(fake.port), "-NoExplorer"],
                                capture_output=True, text=True, timeout=180)
    finally:
        fake.close()
    runs = sorted((tmp_path / "captures").glob("bills_*/"))
    return result, fake, (runs[0] if runs else None), ((runs[0] / "bills_summary.txt").read_text() if runs else "")


@pytest.mark.parametrize("name", ["FinLedger Test Company", "R&D <Ltd> \"Main\" 'Unit'"])
def test_python_bill_request_equals_the_kit_template(name):
    template = (KIT / "bill_export_request.xml").read_bytes()
    assert bill_request(TallyCompany(name, TEST_CO[1])) == template.replace(b"__COMPANY__", xml_text(name).encode())


@needs_pwsh
def test_clean_bill_capture(tmp_path):
    result, fake, run_dir, summary = run(tmp_path, Model(companies=[TEST_CO, OTHER_CO]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary.startswith("verdict: CAPTURED_CLEAN") and "bill_rows: 2 distinct_parties: 1" in summary
    assert "closing_positive: 1 closing_negative: 0 closing_zero: 1" in summary
    assert len(fake.received) == 3
    for req in fake.received:
        assert [e.text for e in ET.fromstring(req).iter("TALLYREQUEST")] == ["Export"]
        assert not re.search(rb"import|action\s*=|<voucher\b|TALLYMESSAGE", req, re.IGNORECASE)
    assert fake.received[1] == bill_request(TallyCompany(*TEST_CO))
    assert (run_dir / "response_02_bills.xml").read_bytes() == fake.sent[1]


@needs_pwsh
@pytest.mark.parametrize("rows,why", [
    ([], "no BILL rows"),
    ([bill("X", "V", "1,180.00", "1180")], "field problems"),
    ([bill("X", "V", "1180", "1180", billdate="1-Apr-26")], "field problems"),
])
def test_bill_differences_are_captured(tmp_path, rows, why):
    result, fake, _, summary = run(tmp_path, Model(rows=rows))
    assert result.returncode == 3 and summary.startswith("verdict: CAPTURED_WITH_DIFFERENCES") and why in summary


@needs_pwsh
def test_bill_capture_never_pins_an_unloaded_company(tmp_path):
    result, fake, _, summary = run(tmp_path, Model(companies=[OTHER_CO]))
    assert result.returncode == 1 and "is not loaded" in summary and len(fake.received) == 1


def test_bill_capture_sends_nothing_itself_and_is_ascii():
    src = (KIT / "Run-Bills-Capture.ps1").read_text(encoding="ascii")
    assert not re.search(r"Invoke-WebRequest|Invoke-RestMethod|WebClient|HttpClient|https?://", src)
    assert "DtdProcessing]::Prohibit" in src and "-Kind bill" in src
    launcher = (KIT / "Run-Bills-Capture.cmd").read_bytes()
    assert b"\r\n" in launcher and b"Run-Bills-Capture.ps1" in launcher
    launcher.decode("ascii")
