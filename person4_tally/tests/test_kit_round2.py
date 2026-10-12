"""Identity-probe kit, round 2: capture.ps1 guards the request STRUCTURE (template text, then the filled request's
tags) so a company name such as 'Shree Imports Pvt Ltd' can be captured, and the Run-*.ps1 scripts refuse a company
name that two loaded companies share ignoring case or spacing (as the Python resolver does).

Every Tally here is a synthetic in-process fake on an ephemeral 127.0.0.1 port. The behavioural tests need
PowerShell (FINLEDGER_PWSH, or pwsh on PATH) and are skipped without it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from finledger_tally.transport import assert_export
from test_identity_probe_kit import KIT, PWSH, _FakeTally, _tally_like
from test_isolation_check import OTHER_CO, TEST_CO, company_list

needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell not available")
CAPTURE_FILES = ("capture.ps1", "company_list_request.xml", "voucher_export_request.xml", "ledger_export_request.xml",
                 "bill_export_request.xml")
WORDY = ["Shree Imports Pvt Ltd", "IMPORT Action = Traders", "Re-Import & <Voucher> \"TallyMessage\" Co's",
         "__TODATE__ Holdings", "Tally__COMPANY__Co"]
TWIN = ("FINLEDGER TEST COMPANY", "1d2c3b4a-5f6e-4a7b-8c9d-0e1f2a3b4c5d")  # synthetic
SPACED_TWIN = ("FinLedger  Test Company", "2e3d4c5b-6a7f-4b8c-9dae-1f2a3b4c5d6e")  # synthetic
OTHER_TWIN = ("OTHER EXISTING COMPANY", "3f4e5d6c-7b8a-4c9d-8ebf-2a3b4c5d6e7f")  # synthetic


def _ok_reply(body: bytes, n: int) -> bytes:
    return "<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER></ENVELOPE>\r\n".encode("utf-16")


def _capture(tmp_path: Path, port: int, kind: str, company: str | None, label: str = "x"):
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "capture.ps1"), "-Kind", kind,
           "-Label", label, "-Port", str(port)]
    if company is not None:
        cmd += ["-Company", company]
    if kind == "voucher":
        cmd += ["-Date", "20260401"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


@pytest.fixture
def kit(tmp_path):
    for name in CAPTURE_FILES:
        shutil.copy(KIT / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def fake():
    f = _FakeTally(b"", responder=_ok_reply)
    yield f
    f.close()


# ---------- capture.ps1: write words in the company NAME are data, not structure ----------


@needs_pwsh
@pytest.mark.parametrize("kind", ["voucher", "ledger", "bill"])
@pytest.mark.parametrize("company", WORDY)
def test_a_company_name_holding_write_words_is_captured_exactly(kit, fake, kind, company):
    result = _capture(kit, fake.port, kind, company)
    assert result.returncode == 0, result.stdout + result.stderr
    [sent] = fake.received
    assert (kit / "request_x.xml").read_bytes() == sent
    root = ET.fromstring(sent)
    assert root.findtext(".//SVCURRENTCOMPANY") == company  # never re-filled: '__TODATE__' stays in the name
    assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]
    template = (kit / f"{kind}_export_request.xml").read_text(encoding="utf-8")
    assert re.findall(r"<[^>]*>", sent.decode("utf-8")) == re.findall(r"<[^>]*>", template)
    assert_export(sent)  # the Python transport guard accepts exactly this request too


@needs_pwsh
@pytest.mark.parametrize("tamper", [
    ("<ID>FinLedgerVoucherProbe</ID>", "<ID>FinLedgerVoucherImport</ID>"),     # a write word in template text
    ("<TYPE>Voucher</TYPE>", "<TYPE>Voucher</TYPE><Voucher/>"),                # a write element, any case
    ("<COLLECTION NAME", "<COLLECTION action = \"Delete\" NAME"),              # an ACTION attribute
    ("<TALLYREQUEST>Export</TALLYREQUEST>", "<TALLYREQUEST>Export</TALLYREQUEST><TALLYREQUEST>Export</TALLYREQUEST>"),
    ("</SVTODATE>", "</SVTODATE><SVOTHER>__OTHER__</SVOTHER>"),                # a placeholder the kind never fills
])
def test_a_tampered_template_is_still_refused_without_sending(kit, fake, tamper):
    path = kit / "voucher_export_request.xml"
    text = path.read_text(encoding="utf-8")
    assert tamper[0] in text
    path.write_text(text.replace(tamper[0], tamper[1], 1), encoding="utf-8")
    result = _capture(kit, fake.port, "voucher", "Plain Co")
    assert result.returncode != 0
    assert "refusing" in result.stdout + result.stderr
    assert fake.received == [] and not (kit / "request_x.xml").exists()


@needs_pwsh
def test_the_company_list_capture_still_works(kit, fake):
    result = _capture(kit, fake.port, "company", None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fake.received == [(KIT / "company_list_request.xml").read_bytes()]


# ---------- Run-*.ps1: a name two loaded companies share ignoring case or spacing is refused ----------


def _run_script(tmp_path: Path, script: str, files: tuple[str, ...], responder, company: str, *extra: str):
    for name in (*files, script):
        shutil.copy(KIT / name, tmp_path / name)
    fake = _FakeTally(b"", responder=responder)
    try:
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(tmp_path / script), "-Company", company,
                                 "-Port", str(fake.port), "-NoExplorer", *extra],
                                capture_output=True, text=True, timeout=180)
    finally:
        fake.close()
    summaries = list((tmp_path / "captures").glob("*/*summary.txt"))
    return result, fake, (summaries[0].read_text() if summaries else "")


def _companies_then_rejected(companies):
    def respond(body: bytes, n: int) -> bytes:
        assert b"<TYPE>Company</TYPE>" in body, "a pinned request was sent for an ambiguous company"
        return company_list(companies)
    return respond


RUNNERS = {
    "bills": ("Run-Bills-Capture.ps1", ("capture.ps1", "company_list_request.xml", "bill_export_request.xml")),
    "masters": ("Run-Masters-Capture.ps1", ("capture.ps1", "company_list_request.xml", "ledger_export_request.xml")),
    "isolation": ("Run-Isolation-Check.ps1", ("capture.ps1", "company_list_request.xml",
                                              "voucher_export_request.xml")),
}


@needs_pwsh
@pytest.mark.parametrize("runner", list(RUNNERS))
@pytest.mark.parametrize("twin", [TWIN, SPACED_TWIN], ids=["case", "spacing"])
def test_runners_refuse_a_name_shared_ignoring_case_before_any_pinned_request(tmp_path, runner, twin):
    script, files = RUNNERS[runner]
    result, fake, summary = _run_script(tmp_path, script, files,
                                        _companies_then_rejected([TEST_CO, twin, OTHER_CO]), TEST_CO[0])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ignoring case or spacing" in summary and "STOPPED" in summary
    assert len(fake.received) == 1  # only the company list; nothing was pinned


@needs_pwsh
def test_isolation_check_refuses_when_any_two_loaded_companies_are_case_twins(tmp_path):
    script, files = RUNNERS["isolation"]
    result, fake, summary = _run_script(tmp_path, script, files,
                                        _companies_then_rejected([TEST_CO, OTHER_CO, OTHER_TWIN]), TEST_CO[0])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "same name ignoring case or spacing" in summary
    assert len(fake.received) == 1


@needs_pwsh
def test_launcher_refuses_a_name_shared_ignoring_case_before_any_voucher_request(tmp_path):
    files = ("capture.ps1", "voucher_export_request.xml", "company_list_request.xml", "Run-FinLedger-Capture.ps1")

    def respond(body: bytes, n: int) -> bytes:
        if b"<TYPE>Company</TYPE>" in body:
            return company_list([TEST_CO, TWIN])
        return _tally_like(body, n)
    result, fake, summary = _run_script(tmp_path, "Run-FinLedger-Capture.ps1", files, respond, TEST_CO[0],
                                        "-EditStep", "skip")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ignoring case or spacing" in summary
    assert len(fake.received) == 1


@needs_pwsh
def test_launcher_still_runs_when_the_name_is_unique_ignoring_case(tmp_path):
    files = ("capture.ps1", "voucher_export_request.xml", "company_list_request.xml", "Run-FinLedger-Capture.ps1")

    def respond(body: bytes, n: int) -> bytes:
        if b"<TYPE>Company</TYPE>" in body:
            return company_list([TEST_CO, OTHER_CO])
        return _tally_like(body, n)
    result, fake, summary = _run_script(tmp_path, "Run-FinLedger-Capture.ps1", files, respond, TEST_CO[0],
                                        "-EditStep", "skip")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(fake.received) == 4 and "STOPPED" not in summary


@pytest.mark.parametrize("script", ["Run-Bills-Capture.ps1", "Run-Masters-Capture.ps1", "Run-Isolation-Check.ps1",
                                    "Run-FinLedger-Capture.ps1"])
def test_every_runner_compares_names_ignoring_case_and_stays_ascii(script):
    src = (KIT / script).read_text(encoding="ascii")
    assert "function Name-Key" in src and "ignoring case or spacing" in src
