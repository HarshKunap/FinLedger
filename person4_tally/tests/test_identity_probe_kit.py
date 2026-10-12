"""Safety checks for the read-only TallyPrime identity capture kit (person4_tally/tally_identity_probe).

The static checks always run. The behavioural checks run capture.ps1 against a fake local Tally and need
PowerShell: `pwsh` on PATH, or FINLEDGER_PWSH pointing at it. Without it they are skipped.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "tally_identity_probe"
TEMPLATE = KIT / "voucher_export_request.xml"
COMPANY_TEMPLATE = KIT / "company_list_request.xml"
SCRIPT = KIT / "capture.ps1"
LAUNCHER = KIT / "Run-FinLedger-Capture.ps1"
KIT_FILES = ("capture.ps1", "voucher_export_request.xml", "company_list_request.xml", "Run-FinLedger-Capture.ps1")
REQUIRED_FIELDS = {"GUID", "MASTERID", "DATE", "VOUCHERTYPENAME", "VOUCHERNUMBER", "PARTYLEDGERNAME", "NARRATION"}
DOC_ID = "9d4f3d5d-499e-41db-b510-e98348e5b503"
PWSH = os.environ.get("FINLEDGER_PWSH") or shutil.which("pwsh") or shutil.which("powershell")


def _filled(company: str = "FinLedger Test Company", date: str = "20260401", to: str | None = None) -> ET.Element:
    text = TEMPLATE.read_text(encoding="utf-8").replace("__COMPANY__", company)
    return ET.fromstring(text.replace("__FROMDATE__", date).replace("__TODATE__", to or date))


# ---------- static: the request template ----------


def test_template_is_a_single_collection_export():
    root = _filled()
    assert root.tag == "ENVELOPE"
    assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]
    assert root.findtext("HEADER/TYPE") == "Collection"


@pytest.mark.parametrize("path,kind", [(TEMPLATE, "Voucher"), (COMPANY_TEMPLATE, "Company")])
def test_templates_have_no_write_constructs(path, kind):
    raw = path.read_text(encoding="utf-8")
    assert not re.search(r"import|action\s*=|<voucher\b|<tallymessage\b", raw, re.IGNORECASE)
    root = ET.fromstring(raw.replace("__COMPANY__", "X").replace("__FROMDATE__", "1").replace("__TODATE__", "1"))
    assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]
    collections = list(root.iter("COLLECTION"))
    assert len(collections) == 1
    assert collections[0].get("ISMODIFY") == "No"
    assert collections[0].findtext("TYPE") == kind


def test_template_pins_company_and_one_day_window():
    raw = TEMPLATE.read_text(encoding="utf-8")
    assert raw.count("__COMPANY__") == 1
    assert raw.count("__FROMDATE__") == raw.count("__TODATE__") == 1
    sv = _filled("Acme &amp; Co", "20260401", "20270331").find("BODY/DESC/STATICVARIABLES")
    assert sv.findtext("SVCURRENTCOMPANY") == "Acme & Co"
    assert (sv.findtext("SVFROMDATE"), sv.findtext("SVTODATE")) == ("20260401", "20270331")


def test_template_fetches_identity_fields():
    fetch = _filled().findtext(".//COLLECTION/FETCH")
    fields = {f.strip() for f in fetch.split(",")}
    assert REQUIRED_FIELDS <= fields


# ---------- static: the script ----------


@pytest.mark.parametrize("path", [SCRIPT, LAUNCHER])
def test_scripts_only_talk_to_loopback_and_hold_no_secrets(path):
    src = path.read_text(encoding="ascii")  # ASCII keeps Windows PowerShell 5.1 from misreading it
    urls = re.findall(r"https?://[^\s\"']+", src)
    assert all(u.startswith("http://127.0.0.1:") for u in urls)
    assert not re.search(r"credential|password|apikey|-Method\s+(Put|Delete|Patch)", src, re.IGNORECASE)
    assert "Set-NetFirewall" not in src and "netsh" not in src.lower()


def test_only_capture_script_sends_requests():
    assert re.findall(r"https?://[^\s\"']+", SCRIPT.read_text(encoding="ascii")) == ["http://127.0.0.1:$Port"]
    launcher = LAUNCHER.read_text(encoding="ascii")
    assert not re.search(r"Invoke-WebRequest|Invoke-RestMethod|WebClient|HttpClient", launcher)


# ---------- behavioural: run capture.ps1 against a fake Tally ----------

needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell not available")


class _FakeTally:
    def __init__(self, reply: bytes, responder=None):
        self.reply = reply
        self.responder = responder
        self.received: list[bytes] = []
        self.sent: list[bytes] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.received.append(body)
                out = fake.responder(body, len(fake.received)) if fake.responder else fake.reply
                fake.sent.append(out)
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def kit(tmp_path):
    for name in KIT_FILES:
        shutil.copy(KIT / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def tally():
    # UTF-16LE with BOM, a CR/LF, a raw &#4; and a rupee sign: any re-encoding or newline change would show.
    fake = _FakeTally("<ENVELOPE><NARRATION>Test ₹ &#4;</NARRATION></ENVELOPE>\r\n".encode("utf-16"))
    yield fake
    fake.close()


def _run(kit: Path, port: int, company="FinLedger Test Company", date="20260401", label="before_edit"):
    cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(kit / "capture.ps1"),
           "-Company", company, "-Date", date, "-Label", label, "-Port", str(port)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _captures(kit: Path) -> set[str]:
    return {p.name for p in kit.iterdir()} - set(KIT_FILES)


@needs_pwsh
def test_capture_saves_raw_reply_and_sends_what_it_saved(kit, tally):
    result = _run(kit, tally.port, company="FinLedger Test Company")
    assert result.returncode == 0, result.stderr
    assert (kit / "response_before_edit.xml").read_bytes() == tally.reply
    sent = (kit / "request_before_edit.xml").read_bytes()
    assert tally.received == [sent]
    assert not sent.startswith(b"\xef\xbb\xbf")
    root = ET.fromstring(sent)
    assert root.findtext(".//SVCURRENTCOMPANY") == "FinLedger Test Company"
    assert root.findtext(".//SVFROMDATE") == root.findtext(".//SVTODATE") == "20260401"
    assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]


@needs_pwsh
def test_company_name_is_escaped_exactly(kit, tally):
    assert _run(kit, tally.port, company="A & B <Test> Co").returncode == 0
    root = ET.fromstring((kit / "request_before_edit.xml").read_bytes())
    assert root.findtext(".//SVCURRENTCOMPANY") == "A & B <Test> Co"


@needs_pwsh
def test_repeat_with_new_label_keeps_earlier_capture(kit, tally):
    assert _run(kit, tally.port, label="before_edit").returncode == 0
    first = (kit / "response_before_edit.xml").read_bytes()
    assert _run(kit, tally.port, label="after_edit").returncode == 0
    assert (kit / "response_before_edit.xml").read_bytes() == first
    assert _captures(kit) == {f"{k}_{l}.xml" for k in ("request", "response") for l in ("before_edit", "after_edit")}


@needs_pwsh
def test_refuses_to_overwrite_an_existing_capture(kit, tally):
    assert _run(kit, tally.port).returncode == 0
    before = (kit / "response_before_edit.xml").read_bytes()
    result = _run(kit, tally.port, company="Other Company")
    assert result.returncode != 0
    assert len(tally.received) == 1
    assert (kit / "response_before_edit.xml").read_bytes() == before


@needs_pwsh
@pytest.mark.parametrize("date", ["20261399", "2026041", "1-Apr-2026", "20260230"])
def test_refuses_bad_dates_without_sending(kit, tally, date):
    assert _run(kit, tally.port, date=date).returncode != 0
    assert tally.received == [] and _captures(kit) == set()


@needs_pwsh
@pytest.mark.parametrize("label", ["..\\..\\evil", "../evil", "a b", ""])
def test_refuses_unsafe_labels_without_sending(kit, tally, label):
    assert _run(kit, tally.port, label=label).returncode != 0
    assert tally.received == [] and _captures(kit) == set()


@needs_pwsh
@pytest.mark.parametrize("tamper", ["<TALLYREQUEST>Import</TALLYREQUEST>", '<VOUCHER ACTION="Create">'])
def test_refuses_a_tampered_template_without_sending(kit, tally, tamper):
    path = kit / "voucher_export_request.xml"
    path.write_text(path.read_text().replace("<TALLYREQUEST>Export</TALLYREQUEST>", tamper))
    assert _run(kit, tally.port).returncode != 0
    assert tally.received == [] and _captures(kit) == set()


@needs_pwsh
def test_unreachable_tally_fails_loudly_and_leaves_nothing(kit, tally):
    port = tally.port
    tally.close()
    result = _run(kit, port)
    assert result.returncode != 0
    assert "Saved" not in result.stdout
    assert _captures(kit) == set()


# ---------- behavioural: the one-click launcher ----------


def _tally_like(body: bytes, n: int, company: str = "FinLedger Test Company") -> bytes:
    """Synthetic replies shaped only for exercising the launcher; NOT a claim about real TallyPrime output."""
    text = body.decode("utf-8")
    if "<TYPE>Company</TYPE>" in text:
        xml = f"<ENVELOPE><COMPANY><NAME>{company}</NAME></COMPANY></ENVELOPE>"
    else:
        start = re.search(r"<SVFROMDATE>(\d+)", text).group(1)
        end = re.search(r"<SVTODATE>(\d+)", text).group(1)
        alter = 5 if n <= 4 else 9  # requests 2-4 are before the edit, 5-6 after
        xml = "<ENVELOPE></ENVELOPE>"
        if start <= "20260401" <= end:
            xml = (f"<ENVELOPE><VOUCHER><GUID>g-1</GUID><MASTERID> 1</MASTERID><ALTERID> {alter}</ALTERID>"
                   f"<NARRATION>Test purchase finledger:document_id={DOC_ID}</NARRATION></VOUCHER></ENVELOPE>")
    return (xml + "\r\n").encode("utf-16")


@pytest.fixture
def tally_like():
    fake = _FakeTally(b"", responder=_tally_like)
    yield fake
    fake.close()


def _launch(kit: Path, port: int, edit: str = "done", stdin: str | None = None):
    cmd = [PWSH, "-NoProfile", "-File", str(kit / "Run-FinLedger-Capture.ps1"),
           "-Port", str(port), "-EditStep", edit, "-NoExplorer"]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=180)


def _runs(kit: Path) -> list[Path]:
    root = kit / "captures"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


@needs_pwsh
def test_launcher_runs_every_read_only_capture_into_one_new_folder(kit, tally_like):
    result = _launch(kit, tally_like.port)
    assert result.returncode == 0, result.stdout + result.stderr
    [run] = _runs(kit)
    labels = ["01_companies", "02_day_before_edit", "03_fy_before_edit", "04_other_day",
              "05_day_after_edit", "06_fy_after_edit"]
    assert sorted(p.name for p in run.iterdir()) == sorted(
        [f"{k}_{l}.xml" for k in ("request", "response") for l in labels] + ["summary.txt"])
    assert len(tally_like.received) == 6
    for label, sent_req, sent_resp in zip(labels, tally_like.received, tally_like.sent):
        assert (run / f"request_{label}.xml").read_bytes() == sent_req
        assert (run / f"response_{label}.xml").read_bytes() == sent_resp
        root = ET.fromstring(sent_req)
        assert [e.text for e in root.iter("TALLYREQUEST")] == ["Export"]
        assert not re.search(rb"import|action\s*=|<voucher\b", sent_req, re.IGNORECASE)
    windows = []
    for req in tally_like.received[1:]:
        root = ET.fromstring(req)
        assert root.findtext(".//SVCURRENTCOMPANY") == "FinLedger Test Company"
        windows.append((root.findtext(".//SVFROMDATE"), root.findtext(".//SVTODATE")))
    assert windows == [("20260401", "20260401"), ("20260401", "20270331"), ("20260402", "20260402"),
                       ("20260401", "20260401"), ("20260401", "20270331")]
    summary = (run / "summary.txt").read_text()
    assert "values_GUID before=[g-1] after=[g-1] -> same" in summary
    assert "values_ALTERID before=[ 5] after=[ 9] -> CHANGED" in summary
    assert "encoding                 UTF-16LE with BOM" in summary
    assert list((kit / "captures").glob("FinLedger_Tally_Capture_run_*.zip"))


@needs_pwsh
@pytest.mark.parametrize("answer,expected", [("DONE\n2000\n", 6), ("skip\n", 4)])
def test_launcher_waits_for_the_manual_edit(kit, tally_like, answer, expected):
    result = _launch(kit, tally_like.port, edit="ask", stdin=answer)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Change ONLY the amount" in result.stdout
    assert len(tally_like.received) == expected
    summary = (_runs(kit)[0] / "summary.txt").read_text()
    assert ("new amount typed by user: 2000" in summary) == (expected == 6)


@needs_pwsh
def test_launcher_never_reuses_a_run_folder(kit, tally_like):
    assert _launch(kit, tally_like.port, edit="skip").returncode == 0
    first = _runs(kit)[0]
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    time.sleep(1.1)
    assert _launch(kit, tally_like.port, edit="skip").returncode == 0
    assert len(_runs(kit)) == 2
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before


@needs_pwsh
def test_launcher_stops_clearly_when_tally_is_closed(kit, tally_like):
    port = tally_like.port
    tally_like.close()
    result = _launch(kit, port)
    assert result.returncode == 1
    assert "STOPPED: TallyPrime is not answering" in result.stdout
    assert "All captures saved" not in result.stdout
    [run] = _runs(kit)
    assert [p.name for p in run.iterdir()] == ["summary.txt"]


@needs_pwsh
def test_launcher_warns_when_company_is_not_loaded(kit):
    fake = _FakeTally(b"", responder=lambda b, n: _tally_like(b, n, company="Some Other Co"))
    try:
        result = _launch(kit, fake.port, edit="skip")
    finally:
        fake.close()
    assert result.returncode == 0
    assert "Could not see 'FinLedger Test Company'" in result.stdout
