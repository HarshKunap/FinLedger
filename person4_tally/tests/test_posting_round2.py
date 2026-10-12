"""Posting round 2: reconcile across revisions, the per-document lock and the gate re-read right before send_started,
no other company's name in the text sent to P3, exact amount matching, one stable prefix for an uncertain send
reported late, the offline validation path, an absolute state folder, private file modes, the preflight's expected
request digest, and a misplaced rejection. SYNTHETIC Tally only (fake_tally.py on 127.0.0.1 ephemeral ports);
nothing here talks to a real Tally."""

from __future__ import annotations

import os
import stat
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    FileJournal,
    Phase,
    PostOutcome,
    State,
    clear_for_resend,
    post_approved,
    reconcile,
)
from finledger_tally import posting as posting_module
from finledger_tally.identity import LookupResult, LookupStatus, VoucherRow
from finledger_tally.posting import check_authorization
from test_hardening import fail_on
from test_isolation_check import TEST_CO
from test_posting import NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 (fixture)
from test_posting_hardening import DAY, MARKER, craft

UNCERTAIN = "outcome_uncertain (reconciliation required; FinLedger will not resend)"
SECRET_A = ("Kapoor Secret Holdings Pvt Ltd", "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
SECRET_B = ("Mehta Confidential Traders LLP", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")


class Gated(FakeTally):
    """Holds the FIRST voucher lookup until ``release`` is set; ``entered`` says it is being held."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered, self.release = threading.Event(), threading.Event()
        self._held = False

    def __call__(self, body: bytes, n: int) -> bytes:
        if b"<TYPE>Voucher</TYPE>" in body and not self._held:
            self._held = True
            self.entered.set()
            assert self.release.wait(10), "test never released the held lookup"
        return super().__call__(body, n)


# =============== 1. reconcile never records an earlier revision's voucher for a later revision ===============


def test_reconcile_does_not_settle_a_newer_revision_with_an_earlier_revisions_voucher(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    clear_for_resend(NEW_DOC, s, actor="ca@example", note="Checked the Day Book: no voucher")
    # r1 did reach Tally after all; r2 (an edit) is approved and its pre-send lookup finds r1's voucher
    fake.add_voucher(TEST_CO[1], narration=f"Purchase {MARKER}")
    fake.import_mode = "create"
    r2 = make_job(revision=2)
    first = post_approved(r2, approval(r2), s)
    assert first.state == State.BLOCKED and "marker_already_in_tally" in dict(first.problems)
    before = phases(s)

    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.erp_id is None
    assert out.erp_result()["ok"] is False
    assert phases(s) == before  # nothing recorded: a person decides which revision that voucher is
    again = post_approved(r2, approval(r2), s)
    assert again.state == State.BLOCKED and again.erp_id is None and fake.imports and len(fake.imports) == 1


def test_reconcile_of_the_same_revision_still_records_that_sends_revision(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    fake.add_voucher(TEST_CO[1], narration=f"Purchase {MARKER}")
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.RECONCILED and out.revision == 1
    rec = FileJournal(s.journal_dir).events(NEW_DOC)[-1]
    send = [e for e in FileJournal(s.journal_dir).events(NEW_DOC) if e.phase == Phase.SEND_STARTED.value][0]
    assert (rec.phase, rec.revision, rec.idempotency_key, rec.job_hash) == (
        Phase.RECONCILED.value, send.revision, send.idempotency_key, send.job_hash)


# =============== 2. one post per document at a time; the gate is re-read right before send_started ===============


def test_two_threads_racing_on_one_document_send_exactly_one_import(tmp_path):
    fake = Gated()
    try:
        s = settings_for(fake, tmp_path)
        results: list[PostOutcome] = []
        first = threading.Thread(target=lambda: results.append(post_approved(make_job(), approval(make_job()), s)))
        first.start()
        assert fake.entered.wait(10)
        busy = post_approved(make_job(), approval(make_job()), s)  # the first holds the document lock
        fake.release.set()
        first.join(10)
        assert busy.state == State.BLOCKED and "document_busy" in dict(busy.problems) and not busy.sent
        assert busy.erp_result() is None  # the job stays with its source
        assert results[0].state == State.POSTED and len(fake.imports) == 1
    finally:
        fake.release.set()
        fake.close()


def test_a_journal_block_written_during_the_pre_send_lookup_stops_the_send(tmp_path):
    """A writer that does not take the lock (an older agent, a hand edit through FileJournal) blocks the document
    while the pre-send lookup runs: the gate is read again before send_started, so nothing is sent."""
    fake = Gated()
    try:
        s = settings_for(fake, tmp_path)
        results: list[PostOutcome] = []
        t = threading.Thread(target=lambda: results.append(post_approved(make_job(), approval(make_job()), s)))
        t.start()
        assert fake.entered.wait(10)
        FileJournal(s.journal_dir).append(NEW_DOC, Phase.OFFLINE_ARTIFACT_ISSUED, revision=1,
                                          idempotency_key=f"{NEW_DOC}:r1", reference="TEST-INV-001",
                                          party_amount="1180.00", detail="issued by another process")
        fake.release.set()
        t.join(10)
        out = results[0]
        assert out.state == State.BLOCKED and "journal_changed" in dict(out.problems) and not out.sent
        assert fake.imports == [] and Phase.SEND_STARTED.value not in phases(s)
    finally:
        fake.release.set()
        fake.close()


# =============== 3. the text sent to P3 never names another loaded company ===============


def _leak_lookup(real):
    """The post-send lookup fails with a reason that lists every loaded company (as identity messages may)."""
    calls = []

    def lookup(transport, name, doc, around, guid=None):
        calls.append(1)
        if len(calls) == 1:
            return real(transport, name, doc, around, guid)
        return LookupResult(LookupStatus.UNREADABLE, f"company not verified: 2 loaded companies are named 'x' "
                            f"(loaded: {SECRET_A[0]!r}, {SECRET_B[0]!r})"), None
    return lookup


def test_a_lookup_failure_after_sending_never_reports_other_company_names_to_p3(tally, tmp_path, monkeypatch):
    fake = tally(companies=(TEST_CO, SECRET_A, SECRET_B))
    s = settings_for(fake, tmp_path)
    monkeypatch.setattr(posting_module, "lookup_document", _leak_lookup(posting_module.lookup_document))
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and len(fake.imports) == 1
    error = out.erp_result()["error"]
    assert error.startswith(UNCERTAIN) and "company not verified" in error
    assert SECRET_A[0] not in error and SECRET_B[0] not in error
    # the late report from the journal block, and reconcile's report, carry no name either
    monkeypatch.undo()
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED
    text = again.erp_result()["error"]
    assert text.startswith(UNCERTAIN) and SECRET_A[0] not in text and SECRET_B[0] not in text
    assert SECRET_A[0] in again.detail  # the full reason stays in the local journal for the person on this PC


def test_an_unknown_import_outcome_with_a_failing_lookup_never_reports_other_company_names(tally, tmp_path,
                                                                                            monkeypatch):
    fake = tally(companies=(TEST_CO, SECRET_A, SECRET_B), import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    monkeypatch.setattr(posting_module, "lookup_document", _leak_lookup(posting_module.lookup_document))
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN
    error = out.erp_result()["error"]
    assert SECRET_A[0] not in error and SECRET_B[0] not in error and "lookup unreadable" in error


def test_reconcile_with_a_company_failure_never_reports_other_company_names(tally, tmp_path, monkeypatch):
    fake = tally(companies=(TEST_CO, SECRET_A, SECRET_B), import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    leak = LookupResult(LookupStatus.UNREADABLE, f"company not verified: (loaded: {SECRET_A[0]!r}, {SECRET_B[0]!r})")
    monkeypatch.setattr(posting_module, "lookup_document", lambda *a, **k: (leak, None))
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN
    assert SECRET_A[0] in out.detail  # kept locally for the person who reconciles on this PC
    error = out.erp_result()["error"]
    assert SECRET_A[0] not in error and SECRET_B[0] not in error


# =============== 4. amounts match exactly ===============


def test_a_voucher_amount_that_differs_past_28_digits_does_not_match():
    def row(amount):
        return VoucherRow(f"{TEST_CO[1]}-00000001", 1, 3, "20260401", "Purchase", "1", "P", "TEST-INV-001",
                          Decimal(amount), MARKER, False)
    assert posting_module._matches(row("-1180.00"), "TEST-INV-001", "1180.00")
    assert not posting_module._matches(row("1180.0000000000000000000000000001"), "TEST-INV-001", "1180.00")
    assert not posting_module._matches(row("-1180.0000000000000000000000000001"), "TEST-INV-001", "1180.00")


# =============== 5. an uncertain send reported late keeps the stable uncertain prefix ===============


def test_an_uncertain_send_whose_report_was_lost_is_reported_later_with_the_uncertain_prefix(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)  # imagine this erp_result never reached P3
    fake.import_mode = "create"
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and "reconciliation_required" in dict(again.problems)
    assert again.erp_result()["error"].startswith(UNCERTAIN) and len(fake.imports) == 1


def test_an_unrecorded_send_is_reported_later_with_the_uncertain_prefix(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS")  # send_started is the last word (the agent died during the Import)
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and again.erp_result()["error"].startswith(UNCERTAIN) and not fake.imports


def test_blocks_without_a_send_in_doubt_keep_the_blocked_prefix(tally, tmp_path):
    fake = tally()
    fake.add_voucher(TEST_CO[1], narration=f"typed by hand {MARKER}")
    s = settings_for(fake, tmp_path)
    first = post_approved(make_job(), approval(make_job()), s)
    assert first.erp_result()["error"].startswith("blocked: ")
    again = post_approved(make_job(), approval(make_job()), s)  # the journal now holds reconciliation_required
    assert again.erp_result()["error"].startswith("blocked: ") and fake.imports == []


# =============== 6. the offline path survives a journal that cannot be written ===============


def test_offline_validation_rejection_with_an_unwritable_journal_returns_instead_of_raising(tally, tmp_path,
                                                                                         monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path, offline_xml_enabled=True)
    other = make_job(doc="7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b")
    assert post_approved(other, approval(other), s).state == State.POSTED  # caches the masters
    fake.down = True
    fail_on(monkeypatch, Phase.VALIDATION_REJECTED)
    bad = make_job(party_ledger="No Such Vendor")
    out = post_approved(bad, approval(bad), s)
    assert out.state == State.VALIDATION_REJECTED and "journal_write_failed" in dict(out.problems)
    assert out.artifact is None and len(fake.imports) == 1


# =============== 7. the state folder must be absolute while posting is enabled ===============


def test_a_relative_state_dir_is_refused_before_any_tally_call(tally, tmp_path, monkeypatch):
    fake = tally()
    monkeypatch.chdir(tmp_path)
    s = settings_for(fake, tmp_path, state_dir=Path("relative_state"))
    assert "state_dir_not_absolute" in dict(check_authorization(make_job(), approval(make_job()), s))
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.NOT_AUTHORIZED and "state_dir_not_absolute" in dict(out.problems)
    assert fake.kinds == [] and not (tmp_path / "relative_state").exists()
    off = settings_for(fake, tmp_path, state_dir=Path("relative_state"), posting_enabled=False)
    assert "state_dir_not_absolute" not in dict(check_authorization(make_job(), approval(make_job()), off))


# =============== 8. private file modes on POSIX ===============


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; the Windows installer sets folder ACLs")
def test_state_files_are_private_and_folders_owner_only(tally, tmp_path):
    old = os.umask(0o022)
    try:
        fake = tally()
        s = settings_for(fake, tmp_path, offline_xml_enabled=True)
        other = make_job(doc="7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b")
        assert post_approved(other, approval(other), s).state == State.POSTED
        fake.down = True
        out = post_approved(make_job(), approval(make_job()), s)
        assert out.state == State.OFFLINE_XML
    finally:
        os.umask(old)

    def mode(p):
        return stat.S_IMODE(os.stat(p).st_mode)

    for f in (s.masters_cache, out.artifact, out.artifact.with_suffix(".json")):
        assert mode(f) == 0o600, f
    for d in (Path(s.state_dir), s.journal_dir, s.journal_dir / NEW_DOC, s.offline_dir, out.artifact.parent):
        assert mode(d) == 0o700, d


# =============== 9. the preflight's expected request digest ===============


def test_an_expected_request_digest_that_does_not_match_sends_nothing(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s, expected_request_sha256="0" * 64)
    assert out.state == State.BLOCKED and "request_changed" in dict(out.problems) and not out.sent
    assert fake.imports == [] and Phase.SEND_STARTED.value not in phases(s) and out.erp_result() is None
    assert out.request_sha256 and out.request_sha256 != "0" * 64

    ok = post_approved(make_job(), approval(make_job()), s, expected_request_sha256=out.request_sha256)
    assert ok.state == State.POSTED and ok.request_sha256 == out.request_sha256 and len(fake.imports) == 1


def test_without_an_expected_digest_posting_is_unchanged(tally, tmp_path):
    fake = tally()
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path), expected_request_sha256=None)
    assert out.state == State.POSTED and len(fake.imports) == 1


# =============== 10. a misplaced rejection is cleared only as a doubt; its revision stays refused ===============


def test_a_misplaced_rejection_cleared_by_a_person_never_lets_the_same_revision_be_sent(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    craft(s, "PR SS OU TR HC")  # TALLY_REJECTED not straight after its own reply: a doubt, then a clearance
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.TALLY_REJECTED and fake.imports == [] and fake.kinds == []
    r2 = make_job(revision=2)  # a corrected revision goes through P3 and is sent (the lookup still runs first)
    assert post_approved(r2, approval(r2), s).state == State.POSTED and len(fake.imports) == 1
