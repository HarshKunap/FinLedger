"""Posting state machine hardening: the ORDER of the pre-send checks, durable send_started and the exact request and
reply bytes, the Import reply under adversarial input, failure injection at the journal and transport boundaries that
test_hardening.py / test_release_boundary.py do not cover, reconciliation, the per-document lock, and the journal
gate's replay rules over phase sequences. SYNTHETIC Tally only (fake_tally.py on 127.0.0.1 ephemeral ports); nothing
here talks to a real Tally, and nothing here shows how a real Tally answers an Import."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import threading
from dataclasses import dataclass, replace
from datetime import date

import pytest

from fake_tally import CREATED, REJECTED, FakeTally
from finledger_tally import (
    FileJournal,
    JournalCorrupt,
    LookupStatus,
    Phase,
    State,
    TallyTransport,
    clear_for_resend,
    lookup_document,
    p3_request_hash,
    parse_import_reply,
    post_approved,
    reconcile,
)
from finledger_tally import journal as journal_module
from finledger_tally import lookup as lookup_module
from finledger_tally import posting as posting_module
from finledger_tally.identity import LookupResult, TallyCompany
from finledger_tally.journal import DocumentBusy, JournalEvent, gate
from finledger_tally.voucher_xml import DraftRejected
from test_hardening import fail_on
from test_isolation_check import OTHER_CO, TEST_CO, voucher
from test_posting import NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 (fixture)
from test_release_boundary import Undelivered

DAY = date(2026, 4, 1)
MARKER = f"finledger:document_id={NEW_DOC}"
IMPOSTOR = (TEST_CO[0], OTHER_CO[1])  # the paired company's name, another company's GUID
_DIGEST, _OTHER_DIGEST = "ab" * 32, "cd" * 32


@pytest.fixture
def make():
    """Start FakeTally or one of its subclasses below; every one is closed after the test."""
    made = []

    def start(cls=FakeTally, **kw):
        fake = cls(**kw)
        made.append(fake)
        return fake

    yield start
    for fake in made:
        fake.close()


def journal_bytes(s) -> bytes:
    path = s.journal_dir / NEW_DOC / "journal.jsonl"
    return path.read_bytes() if path.exists() else b""


def events(s, doc=NEW_DOC) -> list[JournalEvent]:
    return FileJournal(s.journal_dir).events(doc)


def uncertain_send(fake, s) -> None:
    """One real send whose outcome is unknown (connection lost, nothing saved); Tally answers normally afterwards."""
    fake.import_mode = "hangup_before_create"
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and len(fake.imports) == 1
    fake.import_mode = "create"


def marker_voucher(**changes) -> str:
    """A voucher of the paired company carrying NEW_DOC's marker with the job's reference and amount (real shape)."""
    block = voucher(TEST_CO[1], 1, doc=NEW_DOC)
    for old, new in changes.items():
        block = block.replace(*{"date": ('<DATE TYPE="Date">20260401</DATE>', f'<DATE TYPE="Date">{new}</DATE>'),
                                "alter_id": ("> 3</ALTERID>", f"> {new}</ALTERID>")}[old])
    return block


# ---------- crafted journals: the same token language for the pure gate and for journals on disk ----------

_TOKENS = {"PR": Phase.PREVIEW_READY, "VR": Phase.VALIDATION_REJECTED, "SS": Phase.SEND_STARTED,
           "SND": Phase.SEND_NOT_DELIVERED, "RR": Phase.RESPONSE_RECEIVED, "TR": Phase.TALLY_REJECTED,
           "OU": Phase.OUTCOME_UNCERTAIN, "RQ": Phase.RECONCILIATION_REQUIRED, "OF": Phase.OFFLINE_ARTIFACT_ISSUED,
           "PO": Phase.POSTED, "RC": Phase.RECONCILED, "HC": Phase.HUMAN_CLEARED}


def _fields(token: str) -> tuple[Phase, int, dict]:
    """``SS`` = send_started r1, ``SND@2`` = send_not_delivered r2, ``TRx`` = a rejection of ANOTHER request."""
    name, _, rev = token.partition("@")
    phase = _TOKENS[name.rstrip("x")]
    extra: dict = {"request_sha256": _OTHER_DIGEST if name.endswith("x") else _DIGEST}
    if phase in (Phase.SEND_STARTED, Phase.OFFLINE_ARTIFACT_ISSUED):
        extra.update(reference="TEST-INV-001", party_amount="1180.00")
    if phase in (Phase.RESPONSE_RECEIVED, Phase.TALLY_REJECTED):
        extra.update(response_sha256=_OTHER_DIGEST, detail="Voucher totals do not match!")
    if phase in (Phase.POSTED, Phase.RECONCILED):
        extra.update(erp_id=f"{TEST_CO[1]}-00000001", master_id=1)
    if phase is Phase.HUMAN_CLEARED:
        extra.update(actor="ca@example", detail="checked the Day Book")
    return phase, int(rev or 1), extra


def replayed(tokens: str) -> list[JournalEvent]:
    return [JournalEvent(seq=n, at="", phase=phase.value, revision=rev, idempotency_key=f"{NEW_DOC}:r{rev}", **extra)
            for n, (phase, rev, extra) in enumerate(map(_fields, tokens.split()), 1)]


def craft(s, tokens: str, job_hash: str | None = None) -> None:
    """Write ``tokens`` with the real FileJournal.append, so every line is in the journal's own form."""
    j = FileJournal(s.journal_dir)
    for phase, rev, extra in map(_fields, tokens.split()):
        j.append(NEW_DOC, phase, revision=rev, idempotency_key=f"{NEW_DOC}:r{rev}", job_hash=job_hash, **extra)


# =============== 1. the order of the pre-send checks ===============


@dataclass(frozen=True)
class Case:
    faults: tuple[str, ...]  # every check this one job fails
    state: str  # the outcome: the FIRST failing check decides it
    code: str | None
    kinds: tuple[str, ...]  # every Tally request made, in order
    new_phases: tuple[str, ...] = ()  # journal events the call may add


_C, _L, _V = "Company", "Ledger", "Voucher"
ORDER = [
    Case(("unapproved", "journal_corrupt", "bad_draft", "marker_in_tally"), State.NOT_AUTHORIZED, "not_approved", ()),
    Case(("posting_off", "journal_blocked", "impostor_company"), State.NOT_AUTHORIZED, "posting_disabled", ()),
    Case(("p3_posted", "journal_blocked", "impostor_company"), State.ALREADY_POSTED, None, ()),
    Case(("busy", "journal_corrupt", "bad_draft"), State.BLOCKED, "document_busy", ()),
    Case(("journal_corrupt", "impostor_company", "bad_draft"), State.BLOCKED, "journal_unreadable", ()),
    Case(("journal_blocked", "impostor_company", "bad_draft", "marker_in_tally"), State.BLOCKED,
         "reconciliation_required", ()),
    Case(("rejected_same_revision", "impostor_company", "marker_in_tally"), State.TALLY_REJECTED, None, ()),
    Case(("impostor_company", "bad_draft", "marker_in_tally"), State.BLOCKED, "company_unverified", (_C,)),
    Case(("bad_draft", "marker_in_tally"), State.VALIDATION_REJECTED, "missing_ledger", (_C, _C, _L, _C),
         ("validation_rejected",)),
    Case(("journal_unwritable", "marker_in_tally"), State.BLOCKED, "journal_write_failed", (_C, _C, _L, _C)),
    Case(("marker_in_tally",), State.BLOCKED, "marker_already_in_tally", (_C, _C, _L, _C, _C, _V, _C),
         ("preview_ready", "reconciliation_required")),
]


def _arrange(fake, tmp_path, monkeypatch, faults):
    j = make_job(party_ledger="No Such Vendor") if "bad_draft" in faults else make_job()
    auth = approval(j)
    if "unapproved" in faults:
        auth = approval(j, document_status="in_review")
    if "p3_posted" in faults:
        auth = approval(j, document_status="posted", attempt_result="posted", attempt_erp_id="g-1")
    s = settings_for(fake, tmp_path, posting_enabled="posting_off" not in faults)
    if "journal_corrupt" in faults:
        (s.journal_dir / NEW_DOC).mkdir(parents=True)
        (s.journal_dir / NEW_DOC / "journal.jsonl").write_bytes(b'{"seq":1,"phase":"send_sta')
    if "journal_blocked" in faults:
        craft(s, "PR SS OU")
    if "rejected_same_revision" in faults:
        craft(s, "PR SS RR TR")
    if "impostor_company" in faults:
        fake.companies = [IMPOSTOR]
    if "marker_in_tally" in faults:
        fake.add_voucher(TEST_CO[1], narration=f"typed by hand {MARKER}")
    if "journal_unwritable" in faults:
        fail_on(monkeypatch, Phase.PREVIEW_READY)
    held = FileJournal(s.journal_dir).lock(NEW_DOC) if "busy" in faults else None
    return j, auth, s, held


@pytest.mark.parametrize("case", ORDER, ids=["+".join(c.faults) for c in ORDER])
def test_the_first_failing_check_decides_and_nothing_later_runs(tally, tmp_path, monkeypatch, case):
    fake = tally()
    j, auth, s, held = _arrange(fake, tmp_path, monkeypatch, case.faults)
    before = journal_bytes(s)
    try:
        out = post_approved(j, auth, s)
    finally:
        if held:
            held.release()
    assert out.state == case.state, out
    assert case.code is None or case.code in dict(out.problems), out.problems
    assert tuple(fake.kinds) == case.kinds and fake.imports == [] and not out.sent
    if case.new_phases:
        assert journal_bytes(s).startswith(before) and phases(s)[-len(case.new_phases):] == list(case.new_phases)
    else:
        assert journal_bytes(s) == before


def test_a_clean_post_reads_in_a_fixed_order_and_re_checks_the_company_right_before_the_import(tally, tmp_path):
    fake = tally()
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.POSTED
    # company check, pinned masters, pinned marker lookup, ONE import, pinned marker lookup
    assert fake.kinds == [_C, _C, _L, _C, _C, _V, _C, "import", _C, _V, _C]


# =============== 2. durable send_started, and exactly the saved, digested bytes ===============


class Witness(FakeTally):
    """Records what was durable in the journal folder at the moment the Import reached Tally."""

    journal_dir = None

    def _import(self, body, root):
        self.at_import = (FileJournal(self.journal_dir).events(NEW_DOC),
                          {p.name: p.read_bytes() for p in (self.journal_dir / NEW_DOC).glob("*_request.xml")})
        return super()._import(body, root)


def test_send_started_and_the_exact_request_are_durable_before_the_import_and_the_reply_is_kept_whole(make, tmp_path):
    fake = make(Witness)
    s = settings_for(fake, tmp_path)
    fake.journal_dir = s.journal_dir
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.POSTED
    [sent] = fake.imports
    durable, requests = fake.at_import
    assert [e.phase for e in durable] == ["preview_ready", "send_started"]
    preview_ready, send_started = durable
    digest = hashlib.sha256(sent).hexdigest()
    assert preview_ready.request_sha256 == send_started.request_sha256 == out.request_sha256 == digest
    assert requests == {f"{send_started.seq:04d}_request.xml": sent}
    reply = fake.server.sent[fake.kinds.index("import")]
    received = next(e for e in events(s) if e.phase == "response_received")
    saved = s.journal_dir / NEW_DOC / f"{received.seq:04d}_response.xml"
    assert saved.read_bytes() == reply
    assert received.response_sha256 == out.response_sha256 == hashlib.sha256(reply).hexdigest()
    assert events(s)[-1].phase == "posted" and events(s)[-1].response_sha256 == received.response_sha256


def _second_build(monkeypatch, change):
    real, calls = posting_module.preview_purchase_xml, []

    def build(*a, **k):
        calls.append(1)
        p = real(*a, **k)
        return change(p) if len(calls) == 2 else p
    monkeypatch.setattr(posting_module, "preview_purchase_xml", build)


def _refuse(p):
    raise DraftRejected([("invalid_job", "simulated")])


def _one_more_byte(p):
    return replace(p, xml=p.xml + b"\n", sha256=hashlib.sha256(p.xml + b"\n").hexdigest())


@pytest.mark.parametrize("arrange", [
    lambda m: _second_build(m, _one_more_byte),
    lambda m: _second_build(m, _refuse),
    lambda m: m.setattr(posting_module, "preview_purchase_xml",
                        lambda *a, real=posting_module.preview_purchase_xml, **k: replace(real(*a, **k),
                                                                                          sha256="0" * 64)),
], ids=["second_build_differs", "second_build_refused", "digest_not_of_these_bytes"])
def test_a_preview_that_is_not_reproducible_is_never_sent(tally, tmp_path, monkeypatch, arrange):
    fake = tally()
    s = settings_for(fake, tmp_path)
    arrange(monkeypatch)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "preview_not_deterministic" in dict(out.problems)
    assert fake.imports == [] and _V not in fake.kinds and phases(s) == []
    assert out.erp_result()["ok"] is False  # a person must look: this is a defect, not a transient fault


def test_request_bytes_that_do_not_read_back_are_never_sent(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    real = FileJournal.save_bytes

    def flipped(self, doc, name, data):
        if not name.endswith("_request.xml"):
            return real(self, doc, name, data)
        path, digest = real(self, doc, name, data.replace(b"1180.00", b"1180.01"))  # the disk keeps other bytes
        return path, hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(FileJournal, "save_bytes", flipped)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "request_digest_mismatch" in dict(out.problems)
    assert fake.imports == [] and "send_started" not in phases(s)


class SwapCompany(FakeTally):
    """Loads a different company under the same name right after a given request kind was answered."""

    swap_after = None

    def __call__(self, body, n):
        out = super().__call__(body, n)
        if self.kinds and self.kinds[-1] == self.swap_after:
            self.companies = [IMPOSTOR]
        return out


def test_a_company_swapped_during_the_pre_send_lookup_is_caught_before_the_import(make, tmp_path):
    fake = make(SwapCompany)
    fake.swap_after = _V
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "pre-send lookup unreadable" in out.detail
    assert fake.imports == [] and "send_started" not in phases(s)


def test_a_company_swapped_right_after_the_import_is_uncertain_never_posted(make, tmp_path):
    fake = make(SwapCompany)
    fake.swap_after = "import"
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None and out.sent
    fake.companies = [TEST_CO]
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and len(fake.imports) == 1


# =============== 3. Tally's answer, parsed conservatively ===============


class Scripted(FakeTally):
    """``reply`` replaces the Import answer; ``store`` decides whether the voucher is saved; ``after_import`` runs once
    the Import was handled."""

    reply = None
    store = True
    after_import = None

    def _import(self, body, root):
        mode = self.import_mode
        self.import_mode = mode if self.store else "created_not_stored"
        try:
            out = super()._import(body, root)
        finally:
            self.import_mode = mode
        if self.after_import:
            self.after_import(self)
        return self.reply if self.reply is not None else out


def _foreign_row(fake):
    fake.vouchers[TEST_CO[1]] = [voucher(OTHER_CO[1], 1, doc=NEW_DOC)]  # the created voucher, with a foreign GUID


def _unreadable_export(fake):
    fake.vouchers[TEST_CO[1]].append("<VOUCHER><GUID>x</GUID></VOUCHER>")


@pytest.mark.parametrize("reply,store,after", [
    (CREATED.replace(b"{n}", b"1")[:60], True, None),  # cut short
    (REJECTED, True, None),  # says rejected, but the voucher is there
    (REJECTED, False, _unreadable_export),  # says rejected, and the follow-up lookup cannot tell
    ("<RESPONSE><CREATED>¹</CREATED><ERRORS>0</ERRORS></RESPONSE>".encode(), True, None),  # int() would raise
    ("<RESPONSE><CREATED>١</CREATED><ERRORS>0</ERRORS></RESPONSE>".encode(), True, None),  # int() reads it as 1
    (None, True, _foreign_row),  # created, but the voucher row is another company's
], ids=["truncated", "rejected_but_found", "rejected_lookup_unreadable", "superscript_count", "arabic_indic_count",
        "row_of_another_company"])
def test_anything_but_a_clean_create_or_rejection_is_uncertain_and_never_resent(make, tmp_path, reply, store, after):
    fake = make(Scripted)
    fake.reply, fake.store, fake.after_import = reply, store, after
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
    assert out.erp_result()["error"].startswith("outcome_uncertain")
    assert Phase.POSTED.value not in phases(s) and Phase.TALLY_REJECTED.value not in phases(s)
    fake.reply, fake.after_import = None, None
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and len(fake.imports) == 1


@pytest.mark.parametrize("raw", [
    "<RESPONSE><CREATED>١</CREATED><ERRORS>0</ERRORS></RESPONSE>".encode(),
    "<RESPONSE><CREATED>¹</CREATED><ERRORS>0</ERRORS></RESPONSE>".encode(),
    b"<RESPONSE><CREATED>" + b"1" * 5000 + b"</CREATED><ERRORS>0</ERRORS></RESPONSE>",
    b"<RESPONSE><CREATED>+1</CREATED><ERRORS>0</ERRORS></RESPONSE>",
    b"<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>x</LINEERROR><IMPORTRESULT><CREATED>1"
    b"</CREATED></IMPORTRESULT><IMPORTRESULT><CREATED>0</CREATED></IMPORTRESULT></DATA></BODY></ENVELOPE>",
    b"<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>x</LINEERROR><X><IMPORTRESULT><CREATED>1"
    b"</CREATED></IMPORTRESULT></X></DATA></BODY></ENVELOPE>",
    b"<ENVELOPE><HEADER><STATUS>1</STATUS><STATUS>0</STATUS></HEADER><BODY><DATA><IMPORTRESULT><CREATED>1</CREATED>"
    b"<ERRORS>0</ERRORS></IMPORTRESULT></DATA></BODY></ENVELOPE>",
    b"<RESPONSE><LINEERROR>x</LINEERROR><CREATED>0</CREATED><ERRORS>1</ERRORS><X><CREATED>1</CREATED></X></RESPONSE>",
    b"<RESPONSE><CREATED>1</CREATED><ERRORS>0</ERRORS><X><ALTERED>1</ALTERED></X></RESPONSE>",
    b"<RESPONSE><LINEERROR>Ledger missing</LINEERROR><CREATED>0</CREATED><ERRORS>0</ERRORS></RESPONSE>",
], ids=["arabic_indic_digit", "superscript_digit", "5000_digits", "signed_count", "two_import_results",
        "import_result_out_of_place", "two_statuses", "nested_created_in_rejection", "nested_altered_in_create",
        "errors_with_zero_count"])
def test_doubtful_import_replies_are_unclear_and_never_raise(raw):
    assert parse_import_reply(raw).kind == "unclear"


def test_an_unexpected_failure_after_the_reply_is_uncertain_and_recorded(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)

    def broken(raw):
        raise RuntimeError("Shree Ganesh Metals")  # a bug; its text must not travel
    monkeypatch.setattr(posting_module, "parse_import_reply", broken)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
    assert "Shree Ganesh" not in out.detail and "RuntimeError" in out.detail
    assert phases(s)[-1] == "outcome_uncertain"
    monkeypatch.undo()
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and len(fake.imports) == 1


class Exploding(TallyTransport):
    def import_vouchers(self, body: bytes) -> bytes:
        raise RuntimeError("unexpected fault inside the transport")


def test_an_unexpected_transport_fault_is_never_read_as_not_delivered(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s, transport=Exploding(port=fake.port, timeout=2))
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent
    assert Phase.SEND_NOT_DELIVERED.value not in phases(s) and gate(events(s)).status == "blocked"
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and fake.imports == []


# =============== 4. failure injection: the journal ===============


def _cut_to_first_line(data: bytes) -> bytes:
    return data.split(b"\n", 1)[0] + b"\n"


CORRUPTIONS = {
    "torn_last_line": lambda d: d + b'{"seq":4,"phase":"human_cleared"',
    "reformatted_line": lambda d: d.replace(b'"phase":"outcome_uncertain"', b'"phase": "outcome_uncertain"'),
    "duplicated_key": lambda d: d.replace(b'"phase":"outcome_uncertain"',
                                          b'"phase":"outcome_uncertain","phase":"human_cleared"'),
    "revision_retyped": lambda d: d.replace(b'"revision":1', b'"revision":"1"'),
    "seq_renumbered": lambda d: d.replace(b'"seq":3', b'"seq":4'),
    "edited_to_cleared_without_actor": lambda d: d.replace(b'"phase":"outcome_uncertain"',
                                                           b'"phase":"human_cleared"'),
    "edited_to_posted_without_erp_id": lambda d: d.replace(b'"phase":"outcome_uncertain"', b'"phase":"posted"'),
    "unknown_phase": lambda d: d.replace(b'"phase":"outcome_uncertain"', b'"phase":"cleared"'),
    "crlf_line_endings": lambda d: d.replace(b"\n", b"\r\n"),
    "emptied_next_to_its_request_file": lambda d: b"",
}


@pytest.mark.parametrize("corrupt", CORRUPTIONS.values(), ids=CORRUPTIONS.keys())
def test_a_corrupted_journal_blocks_everything_and_never_clears(tally, tmp_path, corrupt):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    path = s.journal_dir / NEW_DOC / "journal.jsonl"
    path.write_bytes(corrupt(path.read_bytes()))
    damaged = path.read_bytes()
    with pytest.raises(JournalCorrupt):
        events(s)
    calls = len(fake.kinds)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_unreadable" in dict(out.problems)
    assert out.erp_result()["ok"] is False  # reported: a person must inspect the journal
    rec = reconcile(NEW_DOC, DAY, s)
    assert rec.state == State.BLOCKED and "journal_unreadable" in dict(rec.problems)
    with pytest.raises(JournalCorrupt):
        clear_for_resend(NEW_DOC, s, actor="ca@example", note="trying to clear a damaged journal")
    assert len(fake.kinds) == calls and len(fake.imports) == 1 and path.read_bytes() == damaged


def test_a_deleted_journal_next_to_its_saved_files_blocks(tally, tmp_path):
    fake = tally(import_mode="garbage")
    s = settings_for(fake, tmp_path)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    (s.journal_dir / NEW_DOC / "journal.jsonl").rename(tmp_path / "moved_away.jsonl")
    fake.import_mode = "create"
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_unreadable" in dict(out.problems) and len(fake.imports) == 1


def test_lost_journal_lines_before_a_saved_reply_block(tally, tmp_path):
    fake = tally(import_mode="garbage")  # preview_ready, send_started, [0003_response.xml], response_received, ...
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    path = s.journal_dir / NEW_DOC / "journal.jsonl"
    path.write_bytes(_cut_to_first_line(path.read_bytes()))  # what is left looks like "never sent"
    fake.import_mode = "create"
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_unreadable" in dict(out.problems) and len(fake.imports) == 1


def _unreadable_folder_is_a_file(s, monkeypatch):
    s.journal_dir.mkdir(parents=True)
    (s.journal_dir / NEW_DOC).write_bytes(b"a file where the document folder should be")


def _unreadable_journal_file(s, monkeypatch):
    real = pathlib.Path.read_bytes

    def read_bytes(self):
        if self.name == "journal.jsonl":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)


def _unlistable_folder(s, monkeypatch):
    real = os.listdir

    def listdir(path="."):
        if str(path).startswith(str(s.journal_dir)):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path)
    monkeypatch.setattr(journal_module.os, "listdir", listdir)


@pytest.mark.parametrize("arrange", [_unreadable_folder_is_a_file, _unreadable_journal_file, _unlistable_folder],
                         ids=["folder_is_a_file", "journal_file_unreadable", "folder_unlistable"])
def test_an_unreadable_journal_location_is_never_read_as_empty(tally, tmp_path, monkeypatch, arrange):
    fake = tally()
    s = settings_for(fake, tmp_path)
    arrange(s, monkeypatch)
    with pytest.raises(JournalCorrupt):
        events(s)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_unreadable" in dict(out.problems)
    rec = reconcile(NEW_DOC, DAY, s)
    assert rec.state == State.BLOCKED and "journal_unreadable" in dict(rec.problems)
    assert fake.kinds == [] and fake.imports == []


def _fail_fsync_of(monkeypatch, phase: Phase):
    """os.fsync fails right after the journal line of ``phase`` was written (the bytes are in the file, not durable)."""
    written, real_write, real_fsync = [], journal_module._write_all, os.fsync

    def write_all(fd, data):
        written.append(bytes(data))
        return real_write(fd, data)

    def fsync(fd):
        if written and f'"phase":"{phase.value}"'.encode() in written[-1]:
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)
    monkeypatch.setattr(journal_module, "_write_all", write_all)
    monkeypatch.setattr(journal_module.os, "fsync", fsync)


def test_fsync_failure_on_send_started_sends_nothing_and_the_document_never_clears(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    _fail_fsync_of(monkeypatch, Phase.SEND_STARTED)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems) and fake.imports == []
    monkeypatch.undo()
    assert phases(s)[-1] == "send_started"  # written but not durable: treated as a send that may have left
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and "reconciliation_required" in dict(again.problems) and fake.imports == []


def test_fsync_failure_on_posted_is_reported_uncertain_and_never_resent(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    _fail_fsync_of(monkeypatch, Phase.POSTED)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None and len(fake.imports) == 1
    monkeypatch.undo()
    again = post_approved(make_job(), approval(make_job()), s)  # the line reached the file: replay honours it
    assert again.state == State.ALREADY_POSTED and again.erp_id == f"{TEST_CO[1]}-00000001" and len(fake.imports) == 1


def _disk_full_while_saving(monkeypatch, suffix: str):
    """The named file gets half its bytes, then ENOSPC."""
    state, real_save, real_write = {"on": False}, FileJournal.save_bytes, journal_module._write_all

    def save(self, doc, name, data):
        state["on"] = name.endswith(suffix)
        try:
            return real_save(self, doc, name, data)
        finally:
            state["on"] = False

    def write_all(fd, data):
        if state["on"]:
            os.write(fd, bytes(data[: len(data) // 2]))
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)
    monkeypatch.setattr(FileJournal, "save_bytes", save)
    monkeypatch.setattr(journal_module, "_write_all", write_all)


def test_disk_full_while_saving_the_reply_is_uncertain_and_the_partial_file_is_never_evidence(tally, tmp_path,
                                                                                               monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    _disk_full_while_saving(monkeypatch, "_response.xml")
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None and out.response_sha256 is None
    monkeypatch.undo()
    assert phases(s)[-1] == "send_started"
    [partial] = (s.journal_dir / NEW_DOC).glob("*_response.xml")
    assert partial.read_bytes() != fake.server.sent[fake.kinds.index("import")]
    assert all(e.response_sha256 is None for e in events(s))  # no event vouches for the partial bytes
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and len(fake.imports) == 1
    settled = reconcile(NEW_DOC, DAY, s)  # the voucher was created: a read-only lookup identifies it
    assert settled.state == State.RECONCILED and settled.erp_id == f"{TEST_CO[1]}-00000001"


def test_disk_full_while_saving_the_request_sends_nothing_and_the_next_delivery_starts_clean(tally, tmp_path,
                                                                                              monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    _disk_full_while_saving(monkeypatch, "_request.xml")
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems) and fake.imports == []
    monkeypatch.undo()
    assert phases(s) == ["preview_ready"]
    assert post_approved(make_job(), approval(make_job()), s).state == State.POSTED and len(fake.imports) == 1


# =============== 5. reconciliation: read-only, one lookup, never a send, never a clear on doubt ===============


def _down(fake):
    fake.down = True


def _garbled_export(fake):
    fake.vouchers[TEST_CO[1]].append("<VOUCHER><GUID>x</GUID></VOUCHER>")


def _foreign_row_in_reply(fake):
    fake.vouchers[TEST_CO[1]].append(voucher(OTHER_CO[1], 7, doc=NEW_DOC))


def _only_in_other_company(fake):
    fake.add_voucher(OTHER_CO[1], narration=f"Test purchase {MARKER}")


@pytest.mark.parametrize("arrange", [_down, _garbled_export, _foreign_row_in_reply, _only_in_other_company],
                         ids=["connection_failure", "unreadable_reply", "foreign_row_in_reply",
                              "match_only_in_another_company"])
def test_reconcile_that_cannot_identify_the_voucher_keeps_the_document_blocked(tally, tmp_path, arrange):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    arrange(fake)
    calls = len(fake.kinds)
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None and "unresolved" in dict(out.problems)
    asked = fake.kinds[calls:]
    assert "import" not in asked and asked.count(_V) <= 1  # one read, never an Import, never a retry
    assert gate(events(s)).status == "blocked"
    fake.down = False
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and len(fake.imports) == 1


def test_reconcile_finds_a_voucher_whose_date_is_far_from_the_draft_date(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    fake.vouchers[TEST_CO[1]] = [marker_voucher(date="20190101")]  # Tally ignores the date window (real capture)
    found, _ = lookup_document(s.transport(), s.company_name, NEW_DOC, DAY, s.company_guid)
    assert found.status is LookupStatus.FOUND and found.row.date == "20190101"
    calls = len(fake.kinds)
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.RECONCILED and out.erp_id == f"{TEST_CO[1]}-00000001"
    assert fake.kinds[calls:] == [_C, _V, _C]  # exactly one lookup, no Import
    assert b"<SVFROMDATE>20260401</SVFROMDATE>" in fake.server.received[-2]  # the window asked for was not widened


def test_reconcile_after_the_voucher_was_edited_keeps_guid_and_master_id(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    fake.vouchers[TEST_CO[1]] = [marker_voucher(alter_id=9)]  # edited in Tally: ALTERID moved on (3 -> 9)
    out = reconcile(NEW_DOC, DAY, s)
    assert (out.state, out.erp_id, out.master_id, out.alter_id) == (
        State.RECONCILED, f"{TEST_CO[1]}-00000001", 1, 9)
    again = post_approved(make_job(), approval(make_job()), s)
    assert (again.state, again.erp_id, again.master_id) == (State.ALREADY_POSTED, out.erp_id, 1)
    assert len(fake.imports) == 1


def test_reconcile_records_the_identity_of_the_send_it_settles(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    fake.vouchers[TEST_CO[1]] = [marker_voucher()]
    assert reconcile(NEW_DOC, DAY, s).state == State.RECONCILED
    send = next(e for e in events(s) if e.phase == "send_started")
    settled = events(s)[-1]
    assert settled.phase == "reconciled"
    assert (settled.revision, settled.job_hash, settled.request_sha256) == (send.revision, send.job_hash,
                                                                            send.request_sha256)


def test_reconcile_never_identifies_a_voucher_by_company_name_alone(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    fake.companies = [IMPOSTOR]  # same name, another company, holding a voucher with this marker
    fake.vouchers[IMPOSTOR[1]] = [voucher(IMPOSTOR[1], 1, doc=NEW_DOC)]
    calls = len(fake.kinds)
    out = reconcile(NEW_DOC, DAY, settings_for(fake, tmp_path, company_guid=None))
    assert out.state == State.BLOCKED and "not_paired" in dict(out.problems) and out.erp_id is None
    assert len(fake.kinds) == calls and gate(events(s)).status == "blocked"
    paired = reconcile(NEW_DOC, DAY, s)  # paired: the GUID check refuses the impostor
    assert paired.state == State.OUTCOME_UNCERTAIN and paired.erp_id is None


@pytest.mark.parametrize("around", [None, "2026-04-01", 20260401])
def test_reconcile_refuses_a_date_that_is_not_a_date_without_reading(tally, tmp_path, around):
    fake = tally()
    s = settings_for(fake, tmp_path)
    uncertain_send(fake, s)
    calls = len(fake.kinds)
    out = reconcile(NEW_DOC, around, s)
    assert out.state == State.BLOCKED and "invalid_date" in dict(out.problems) and len(fake.kinds) == calls


def test_reconcile_never_settles_a_marker_that_finledger_did_not_send(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    fake.add_voucher(TEST_CO[1], narration=f"typed by hand {MARKER}")
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED  # marker_already_in_tally
    before, calls = journal_bytes(s), len(fake.kinds)
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None and "no FinLedger send" in out.detail
    assert journal_bytes(s) == before and len(fake.kinds) == calls and fake.imports == []


def test_reconcile_never_matches_a_send_that_never_left(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    undelivered = post_approved(make_job(), approval(make_job()), s, transport=Undelivered(port=fake.port, timeout=2))
    assert undelivered.state == State.SEND_NOT_DELIVERED
    fake.add_voucher(TEST_CO[1], narration=f"typed by hand {MARKER}")  # same reference and amount as that send
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None
    assert Phase.RECONCILED.value not in phases(s) and fake.imports == []


def test_reconcile_with_two_unresolved_sends_leaves_the_choice_to_a_person(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS SS")
    fake.vouchers[TEST_CO[1]] = [marker_voucher()]
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and "2 sends are unresolved" in out.detail and fake.kinds == []


def test_a_journal_posted_record_of_another_company_is_never_returned_as_posted(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    j = make_job()
    FileJournal(s.journal_dir).append(NEW_DOC, Phase.POSTED, revision=1, idempotency_key=j["idempotency_key"],
                                      job_hash=p3_request_hash(j), erp_id=f"{OTHER_CO[1]}-00000001", master_id=1)
    out = post_approved(j, approval(j), s)
    assert out.state == State.BLOCKED and "already_posted_other" in dict(out.problems) and out.erp_id is None
    assert out.erp_result()["ok"] is False and fake.kinds == []
    rec = reconcile(NEW_DOC, DAY, s)
    assert rec.state == State.BLOCKED and rec.erp_id is None


# =============== 6. one post, reconciliation or clearance of a document at a time ===============


class Gated(FakeTally):
    """Holds the Import until the test releases it, so another call can run while it is in flight."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered, self.release = threading.Event(), threading.Event()

    def _import(self, body, root):
        self.entered.set()
        self.release.wait(5)
        return super()._import(body, root)


def test_a_second_call_while_an_import_is_in_flight_is_refused_without_any_read(make, tmp_path):
    fake = make(Gated)
    s = settings_for(fake, tmp_path, timeout=10)
    results = []
    first = threading.Thread(target=lambda: results.append(post_approved(make_job(), approval(make_job()), s)))
    first.start()
    try:
        assert fake.entered.wait(5)
        calls, before = len(fake.kinds), journal_bytes(s)
        busy = post_approved(make_job(), approval(make_job()), s)
        assert busy.state == State.BLOCKED and "document_busy" in dict(busy.problems) and busy.erp_result() is None
        assert "document_busy" in dict(reconcile(NEW_DOC, DAY, s).problems)
        with pytest.raises(DocumentBusy):
            clear_for_resend(NEW_DOC, s, actor="ca@example", note="clearing while a send is in flight")
        assert len(fake.kinds) == calls and journal_bytes(s) == before
    finally:
        fake.release.set()
        first.join(10)
    assert results[0].state == State.POSTED and len(fake.imports) == 1
    assert post_approved(make_job(), approval(make_job()), s).state == State.ALREADY_POSTED  # the lock was released


def test_the_lock_is_released_when_a_post_fails_unexpectedly(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)

    def broken(*a, **k):
        raise RuntimeError("bug before any send")
    monkeypatch.setattr(posting_module, "current_company", broken)
    with pytest.raises(RuntimeError):
        post_approved(make_job(), approval(make_job()), s)
    monkeypatch.undo()
    assert fake.imports == []
    assert post_approved(make_job(), approval(make_job()), s).state == State.POSTED


# =============== 7. malformed approval records refuse, never crash ===============


def _deep(depth=100_000):
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize("changes,code", [
    ({"approvals": (("human",),)}, "approval_missing"),
    ({"approvals": ("human",)}, "approval_missing"),
    ({"approvals": ((1, 1),)}, "approval_missing"),
    ({"superseded": None}, "approval_superseded"),
    ({"superseded": (("human",),)}, "approval_superseded"),
])
def test_malformed_approval_rows_are_refused_before_any_call(tally, tmp_path, changes, code):
    fake = tally()
    j = make_job()
    out = post_approved(j, approval(j, **changes), settings_for(fake, tmp_path))
    assert out.state == State.NOT_AUTHORIZED and code in dict(out.problems) and fake.kinds == []


def test_a_payload_too_deep_to_hash_is_refused_not_crashed(tally, tmp_path):
    fake = tally()
    j = make_job()
    auth = approval(j)
    j["voucher_draft"]["reasons"] = _deep()
    out = post_approved(j, auth, settings_for(fake, tmp_path))
    assert out.state == State.NOT_AUTHORIZED and "job_changed" in dict(out.problems) and fake.kinds == []


# =============== 8. the lookup itself ===============


def test_lookup_refuses_a_non_date_before_any_request(tally):
    fake = tally()
    with pytest.raises(TypeError):
        lookup_document(TallyTransport(port=fake.port, timeout=2), TEST_CO[0], NEW_DOC, "2026-04-01", TEST_CO[1])
    assert fake.kinds == []


def test_a_lookup_request_refused_by_the_guards_is_unreadable_never_not_found(tally, monkeypatch):
    fake = tally()
    monkeypatch.setattr(lookup_module, "voucher_request", lambda company, a, b: b"not a pinned export")
    result, company = lookup_document(TallyTransport(port=fake.port, timeout=2), TEST_CO[0], NEW_DOC, DAY,
                                      TEST_CO[1])
    assert result.status is LookupStatus.UNREADABLE and company is None and _V not in fake.kinds


# =============== 9. gate replay rules over phase sequences ===============

GATE_TABLE = [
    ("", "clear"),
    ("PR", "clear"),
    ("VR", "clear"),
    ("HC", "clear"),
    ("SND", "clear"),  # "not delivered" with no send before it proves nothing and blocks nothing
    ("SS", "blocked"),
    ("PR SS SND", "clear"),
    ("SS SND SND", "clear"),
    ("SS RR SND", "blocked"),  # a reply came back: "not delivered" cannot follow it
    ("SS PR SND", "blocked"),  # not the very next event
    ("SS SS SND", "blocked"),  # the first send is still unresolved
    ("SS OU", "blocked"),
    ("SS OU SND", "blocked"),
    ("SS OU HC", "clear"),
    ("SS OU HC SND", "clear"),
    ("SS OU SS@2 SND@2", "blocked"),  # a later undelivered send never erases an earlier doubt
    ("SS OU TR", "blocked"),  # a rejection after doubt answers no send in flight
    ("SS RR TR", "rejected"),
    ("SS RR TRx", "blocked"),  # rejection of another request
    ("SS RR TR@2", "blocked"),  # rejection of another revision
    ("SS RR RR TR", "blocked"),
    ("TR", "blocked"),
    ("RR", "blocked"),
    ("SS RR TR PR@2", "rejected"),  # a newer revision may be sent (post_approved compares revisions)
    ("SS RR TR SS@2 SND@2", "rejected"),
    ("SS RR TR SS@2 OU@2 HC@2", "rejected"),
    ("SS RR PO", "posted"),
    ("SS RR PO SS OU", "posted"),  # nothing reopens a posted document
    ("SS OU RC", "posted"),
    ("SS RR OU RC HC", "posted"),
    ("OF", "blocked"),
    ("OF SND", "blocked"),
    ("OF HC", "clear"),
    ("OF RC", "posted"),
    ("RQ", "blocked"),
    ("RQ HC", "clear"),
    ("SS OU HC SS RR OU", "blocked"),
    ("TR HC", "rejected"),  # a misplaced rejection is a doubt AND keeps its revision refused after a clearance
]


@pytest.mark.parametrize("tokens,status", GATE_TABLE, ids=[t or "empty" for t, _ in GATE_TABLE])
def test_gate_replay_rules(tokens, status):
    assert gate(replayed(tokens)).status == status


@pytest.mark.parametrize("tokens,unresolved", [
    ("SS OU", ("SS",)), ("OF", ("OF",)), ("RQ", ()), ("SS RR TR", ()), ("SS SS SND", ("SS",)), ("SS OU HC", ()),
    ("SS SND", ()), ("PR SS RR", ("SS",)),
])
def test_gate_names_the_sends_still_unresolved(tokens, unresolved):
    pending = gate(replayed(tokens)).unresolved
    assert tuple({"send_started": "SS", "offline_artifact_issued": "OF"}[e.phase] for e in pending) == unresolved
    if pending:
        assert pending[0].seq == next(n for n, t in enumerate(tokens.split(), 1) if t in unresolved)


DOUBTFUL = ["SS", "SS SS SND", "SS OU SS@2 SND@2", "SS OU TR", "SS RR SND", "SS RR TRx", "SS RR RR TR", "TR", "RR",
            "OF", "OF SND", "RQ", "SS OU HC SS RR OU"]


@pytest.mark.parametrize("tokens", DOUBTFUL)
@pytest.mark.parametrize("revision", [1, 2])
def test_a_doubtful_journal_on_disk_blocks_every_revision_before_any_tally_call(tally, tmp_path, tokens, revision):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, tokens)
    j = make_job(revision=revision)
    out = post_approved(j, approval(j), s)
    assert out.state == State.BLOCKED and fake.kinds == [] and fake.imports == []


@pytest.mark.parametrize("tokens", ["PR SS SND", "PR SS OU HC", "OF HC", "PR RQ HC"])
def test_a_settled_journal_on_disk_may_send_and_the_marker_lookup_still_runs_first(tally, tmp_path, tokens):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, tokens)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.POSTED and len(fake.imports) == 1
    assert fake.kinds.index(_V) < fake.kinds.index("import")


def test_a_rejected_revision_is_never_resent_and_a_newer_one_may_be(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS RR TR PR@2 SS@2 SND@2")  # r1 rejected; an r2 send never left
    same = post_approved(make_job(), approval(make_job()), s)
    assert same.state == State.TALLY_REJECTED and fake.kinds == []
    newer = make_job(revision=2)
    assert post_approved(newer, approval(newer), s).state == State.POSTED and len(fake.imports) == 1


# =============== 10. the journal's own line format ===============


def test_journal_lines_are_exactly_what_append_writes(tmp_path):
    j = FileJournal(tmp_path)
    j.append(NEW_DOC, Phase.PREVIEW_READY, revision=1, idempotency_key=f"{NEW_DOC}:r1", request_sha256=_DIGEST)
    line = (tmp_path / NEW_DOC / "journal.jsonl").read_bytes()
    raw = json.loads(line)
    assert line == json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert [e.phase for e in j.events(NEW_DOC)] == ["preview_ready"]


@pytest.mark.parametrize("field,value", [("request_sha256", "not-a-digest"), ("master_id", True), ("revision", 0),
                                         ("detail", None), ("erp_id", 5)])
def test_a_field_of_the_wrong_type_makes_the_journal_corrupt(tmp_path, field, value):
    j = FileJournal(tmp_path)
    j.append(NEW_DOC, Phase.PREVIEW_READY, revision=1, idempotency_key=f"{NEW_DOC}:r1")
    path = tmp_path / NEW_DOC / "journal.jsonl"
    raw = json.loads(path.read_bytes())
    raw[field] = value
    path.write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    with pytest.raises(JournalCorrupt):
        j.events(NEW_DOC)


# =============== 11. a send a person cleared by mistake can still be reconciled ===============


class CreatedThenDown(FakeTally):
    """Tally saves the voucher, the connection drops, and Tally stays unreachable for the follow-up lookup."""

    def _import(self, body, root):
        self.import_mode = "create"
        super()._import(body, root)
        self.down = True
        raise ConnectionResetError("simulated: dropped after saving")


def test_a_send_cleared_by_mistake_is_reconciled_once_its_voucher_is_found(make, tmp_path):
    fake = make(CreatedThenDown)
    s = settings_for(fake, tmp_path)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    fake.down = False
    clear_for_resend(NEW_DOC, s, actor="ca@example", note="looked in the Day Book, nothing there (wrong)")
    second = post_approved(make_job(), approval(make_job()), s)
    assert second.state == State.BLOCKED and "marker_already_in_tally" in dict(second.problems)
    rec = reconcile(NEW_DOC, DAY, s)
    assert rec.state == State.RECONCILED and rec.erp_id == f"{TEST_CO[1]}-00000001"
    send = next(e for e in events(s) if e.phase == "send_started")
    assert rec.request_sha256 == send.request_sha256
    third = post_approved(make_job(), approval(make_job()), s)
    assert third.state == State.ALREADY_POSTED and third.erp_id == rec.erp_id
    assert len(fake.imports) == 1


def test_an_offline_file_cleared_by_mistake_is_reconciled_once_its_voucher_is_found(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR OF HC", job_hash=p3_request_hash(make_job()))  # a CA imported it; a person wrongly said nobody did
    fake.vouchers[TEST_CO[1]] = [marker_voucher()]
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED
    assert reconcile(NEW_DOC, DAY, s).state == State.RECONCILED
    assert post_approved(make_job(), approval(make_job()), s).state == State.ALREADY_POSTED and fake.imports == []


def test_a_cleared_send_is_never_matched_to_a_voucher_that_differs_from_it(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS OU HC RQ")
    fake.add_voucher(TEST_CO[1], narration=f"typed by hand {MARKER}", amount="999.00")
    before = journal_bytes(s)
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None and "does not match" in out.detail
    assert journal_bytes(s) == before and fake.imports == []


def test_two_cleared_sends_leave_the_choice_to_a_person(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS OU HC SS OU HC RQ")
    fake.vouchers[TEST_CO[1]] = [marker_voucher()]
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and "2 sends are unresolved" in out.detail and fake.kinds == []


def test_an_unresolved_send_wins_over_a_cleared_one(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS OU HC SS@2 OU@2")
    fake.vouchers[TEST_CO[1]] = [marker_voucher()]
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.RECONCILED and out.revision == 2 and fake.imports == []


@pytest.mark.parametrize("tokens,cleared", [
    ("SS OU HC", ("SS",)), ("OF HC", ("OF",)), ("SS SND", ()), ("SS RR TR", ()), ("SS SND HC", ()),
    ("SS OU HC SS SND", ("SS",)), ("SS SS SND HC", ("SS",)), ("SS OU HC SS OU HC", ("SS", "SS")), ("SS OU", ()),
    ("RQ HC", ()),
])
def test_gate_names_the_sends_only_a_person_settled(tokens, cleared):
    assert tuple({"send_started": "SS", "offline_artifact_issued": "OF"}[e.phase]
                 for e in gate(replayed(tokens)).cleared) == cleared


# =============== 12. a misplaced rejection keeps its revision refused ===============


@pytest.mark.parametrize("tokens,status,revision", [
    ("TR HC", "rejected", 1),
    ("SS OU TR HC", "rejected", 1),
    ("SS RR TRx HC", "rejected", 1),
    ("TR@2 SS RR TR", "blocked", 2),  # the highest rejected revision is kept
    ("TR@2 HC SS RR TR", "rejected", 2),
])
def test_a_rejection_that_answers_no_send_still_refuses_its_revision_after_a_clearance(tokens, status, revision):
    g = gate(replayed(tokens))
    assert g.status == status and g.rejected.revision == revision


def test_a_misplaced_rejection_on_disk_refuses_its_revision_after_a_clearance(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS OU TR HC")
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.TALLY_REJECTED and fake.kinds == [] and fake.imports == []


# =============== 13. the company re-check right before the Import (defence in depth) ===============


def test_a_pre_send_lookup_against_another_company_blocks_the_send(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)

    def other_company(transport, name, doc, around, guid=None):
        return LookupResult(LookupStatus.NOT_FOUND, "no voucher carries the marker"), TallyCompany(*OTHER_CO)

    monkeypatch.setattr(posting_module, "lookup_document", other_company)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "company_changed" in dict(out.problems)
    assert fake.imports == [] and Phase.SEND_STARTED.value not in phases(s)
