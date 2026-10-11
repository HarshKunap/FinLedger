"""Release-readiness checks: the approval-to-posting boundary, every persistence boundary of the posting state machine,
the operator CLI, agent shutdown in each phase, and bounded logs. SYNTHETIC Tally only (fake_tally.py); nothing here
talks to a real Tally or sends Import XML anywhere but a local test server."""

from __future__ import annotations

import io
import json
import logging
import threading
from datetime import date

import pytest

from finledger_tally import (
    AgentHost,
    ClaimedJob,
    FileJournal,
    Phase,
    PostAuthorization,
    State,
    TallyConnector,
    TallyTransport,
    TallyUnavailable,
    clear_for_resend,
    post_approved,
    reconcile,
)
from finledger_tally import posting as posting_module
from finledger_tally.__main__ import main
from test_hardening import Sink, Source, claimed, fail_on
from test_isolation_check import OTHER_CO, TEST_CO
from test_posting import CLIENT, NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 (fixture)

OTHER_CLIENT = "55555555-5555-4555-8555-555555555555"
OTHER_DOC = "7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"
DAY = date(2026, 4, 1)


def rows(j, *, doc_status="approved", result="pending", erp_id=None, approvals=None, client=CLIENT, doc=None):
    """P2 rows exactly as ``from_rows`` reads them (documents, posting_attempts, approvals)."""
    doc = doc or j["document_id"]
    document = {"id": doc, "client_id": client, "status": doc_status}
    attempt = {"client_id": client, "document_id": doc, "result": result, "revision": j["revision"],
               "idempotency_key": j["idempotency_key"], "request_hash": posting_module.p3_request_hash(j),
               "erp_id": erp_id}
    if approvals is None:
        approvals = [{"client_id": client, "document_id": doc, "kind": "human", "revision": j["revision"],
                      "superseded_at": None}]
    return document, attempt, approvals


def approval_row(kind="human", revision=1, *, doc=NEW_DOC, client=CLIENT, superseded=None):
    return {"client_id": client, "document_id": doc, "kind": kind, "revision": revision, "superseded_at": superseded}


# ====================== 1. approval-to-posting boundary ======================


def _refused(fake, out, code):
    assert out.state == State.NOT_AUTHORIZED, out
    assert code in dict(out.problems), out.problems
    assert fake.kinds == [] and fake.imports == [] and out.erp_result() is None


def test_missing_approval_never_reaches_tally(tally, tmp_path):
    fake, j = tally(), make_job()
    _refused(fake, post_approved(j, PostAuthorization.from_rows(CLIENT, *rows(j, approvals=[])),
                                 settings_for(fake, tmp_path)), "approval_missing")


@pytest.mark.parametrize("status", ["rejected", "resubmit", "in_review", "exception", "tax_checked"])
def test_a_rejected_or_unapproved_document_never_reaches_tally(tally, tmp_path, status):
    fake, j = tally(), make_job()
    _refused(fake, post_approved(j, PostAuthorization.from_rows(CLIENT, *rows(j, doc_status=status)),
                                 settings_for(fake, tmp_path)), "not_approved")


def test_a_stale_approval_for_an_older_revision_is_refused(tally, tmp_path):
    fake, j = tally(), make_job(revision=2)
    auth = PostAuthorization.from_rows(CLIENT, *rows(j, approvals=[approval_row("human", 1)]))
    _refused(fake, post_approved(j, auth, settings_for(fake, tmp_path)), "approval_missing")


def test_a_superseded_approval_is_refused_even_for_the_same_revision(tally, tmp_path):
    fake, j = tally(), make_job()
    auth = PostAuthorization.from_rows(CLIENT, *rows(j, approvals=[approval_row(superseded="2026-10-10T10:00Z")]))
    assert auth.approvals == () and auth.superseded == (("human", 1),)
    out = post_approved(j, auth, settings_for(fake, tmp_path))
    _refused(fake, out, "approval_superseded")
    assert "approval_missing" in dict(out.problems)


@pytest.mark.parametrize("revisions", [(1,), (1, 2)])
def test_a_payment_approval_is_not_an_invoice_approval(tally, tmp_path, revisions):
    fake, j = tally(), make_job()
    auth = PostAuthorization.from_rows(CLIENT, *rows(j, approvals=[approval_row("payment_release", r)
                                                                   for r in revisions]))
    _refused(fake, post_approved(j, auth, settings_for(fake, tmp_path)), "approval_missing")


@pytest.mark.parametrize("payload", [
    {"document_id": NEW_DOC, "client_id": CLIENT},  # an extract/score job payload, not a post job
    {"document_id": NEW_DOC, "client_id": CLIENT, "revision": 1, "idempotency_key": f"{NEW_DOC}:r1"},  # no draft
])
def test_a_job_for_another_action_is_refused(tally, tmp_path, payload):
    fake, j = tally(), make_job()
    auth = PostAuthorization.from_rows(CLIENT, *rows(j))  # a genuine approval of the real post job
    _refused(fake, post_approved(payload, auth, settings_for(fake, tmp_path)), "job_changed")


@pytest.mark.parametrize("method", ["post_payment", "post_credit_debit"])
def test_payment_and_note_actions_are_refused_without_a_call(tally, tmp_path, method):
    fake, j = tally(), make_job()
    out = getattr(TallyConnector(settings_for(fake, tmp_path)), method)(j, approval(j))
    assert out.state == State.VALIDATION_REJECTED and fake.kinds == []


def test_a_payment_draft_cannot_ride_the_invoice_path(tally, tmp_path):
    fake, j = tally(), make_job(voucher_type="payment")
    out = TallyConnector(settings_for(fake, tmp_path)).post_invoice(j, approval(j))
    assert out.state == State.VALIDATION_REJECTED and "unsupported_voucher_type" in dict(out.problems)
    assert fake.kinds == []


def test_an_approval_for_a_different_document_is_refused(tally, tmp_path):
    fake = tally()
    other = make_job(doc=OTHER_DOC)
    auth = PostAuthorization.from_rows(CLIENT, *rows(other))  # rows of OTHER_DOC, all consistent
    out = post_approved(make_job(), auth, settings_for(fake, tmp_path))
    _refused(fake, out, "document_mismatch")
    assert "job_changed" in dict(out.problems)


def test_rows_of_two_documents_cannot_be_combined():
    j = make_job()
    document, attempt, _ = rows(j)
    with pytest.raises(ValueError, match="same document"):
        PostAuthorization.from_rows(CLIENT, document, attempt, [approval_row(doc=OTHER_DOC)])
    with pytest.raises(ValueError, match="same document"):
        PostAuthorization.from_rows(CLIENT, document, dict(attempt, document_id=OTHER_DOC), [approval_row()])


def test_a_row_without_a_required_column_is_refused_not_guessed():
    document, attempt, approvals = rows(make_job())
    with pytest.raises(ValueError, match="required column"):
        PostAuthorization.from_rows(CLIENT, {k: v for k, v in document.items() if k != "id"}, attempt, approvals)
    with pytest.raises(ValueError, match="required column"):
        PostAuthorization.from_rows(CLIENT, document, {k: v for k, v in attempt.items() if k != "erp_id"}, approvals)
    with pytest.raises(ValueError, match="required column"):
        PostAuthorization.from_rows(CLIENT, document, attempt, [{"kind": "human", "revision": 1}])


def test_cross_client_rows_and_settings_are_refused(tally, tmp_path):
    fake, j = tally(), make_job()
    with pytest.raises(ValueError, match="same client"):
        PostAuthorization.from_rows(CLIENT, *rows(j, approvals=[approval_row(client=OTHER_CLIENT)]))
    with pytest.raises(ValueError, match="same client"):
        PostAuthorization.from_rows(OTHER_CLIENT, *rows(j))
    foreign = PostAuthorization.from_rows(OTHER_CLIENT, *rows(j, client=OTHER_CLIENT))  # all rows of another client
    _refused(fake, post_approved(j, foreign, settings_for(fake, tmp_path)), "client_mismatch")
    agent_of_other = settings_for(fake, tmp_path, client_id=OTHER_CLIENT)
    _refused(fake, post_approved(j, PostAuthorization.from_rows(CLIENT, *rows(j)), agent_of_other), "client_mismatch")


@pytest.mark.parametrize("companies,guid", [
    ([("FinLedger Test Company", OTHER_CO[1])], TEST_CO[1]),  # same name, another company's GUID
    ([OTHER_CO], TEST_CO[1]),  # the paired company is not loaded
    ([TEST_CO, (TEST_CO[0], OTHER_CO[1])], TEST_CO[1]),  # two loaded companies share the name
])
def test_cross_company_never_imports(tally, tmp_path, companies, guid):
    fake, j = tally(companies=companies), make_job()
    out = post_approved(j, approval(j), settings_for(fake, tmp_path, company_guid=guid))
    assert out.state == State.BLOCKED and "company_unverified" in dict(out.problems)
    assert fake.imports == [] and set(fake.kinds) == {"Company"}  # never pinned to an unverified company
    assert phases(settings_for(fake, tmp_path)) == []


def test_duplicate_delivery_posts_once(tally, tmp_path):
    fake = tally()
    c = claimed(9)
    sink = Sink()
    host = AgentHost(settings_for(fake, tmp_path), Source([c, c, c]), sink)
    host.startup()
    states = [host.tick().outcome.state for _ in range(3)]
    assert states == [State.POSTED, State.ALREADY_POSTED, State.ALREADY_POSTED] and len(fake.imports) == 1
    assert len({r[1]["erp_id"] for r in sink.reports}) == 1


def test_already_posted_in_p3_returns_the_recorded_id_without_tally(tally, tmp_path):
    fake, j = tally(), make_job()
    auth = PostAuthorization.from_rows(CLIENT, *rows(j, doc_status="posted", result="posted", erp_id="guid-1"))
    out = post_approved(j, auth, settings_for(fake, tmp_path))
    assert (out.state, out.erp_id, fake.kinds) == (State.ALREADY_POSTED, "guid-1", [])


def test_an_uncertain_previous_attempt_blocks_the_next_delivery(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s, j = settings_for(fake, tmp_path), make_job()
    assert post_approved(j, approval(j), s).state == State.OUTCOME_UNCERTAIN
    fake.import_mode = "create"
    again = post_approved(j, approval(j), s)
    assert again.state == State.BLOCKED and "reconciliation_required" in dict(again.problems)
    assert len(fake.imports) == 1 and again.erp_result()["ok"] is False


def test_agent_restart_keeps_the_boundary(tally, tmp_path):
    fake = tally(import_mode="hangup_after_create")
    s = settings_for(fake, tmp_path)
    first = AgentHost(s, Source([claimed(1)]), Sink())
    found = first.run(interval=0.01, max_ticks=1)[0].outcome  # the read-only lookup after the hang-up found it
    assert found.state == State.RECONCILED and found.erp_id
    fake.import_mode = "create"
    second = AgentHost(s, Source([claimed(1)]), Sink())  # a new process on the same state folder
    out = second.run(interval=0.01, max_ticks=1)[0].outcome
    assert (out.state, out.erp_id) == (State.ALREADY_POSTED, found.erp_id) and len(fake.imports) == 1


# ====================== 2. every persistence boundary ======================


class Undelivered(TallyTransport):
    def import_vouchers(self, body: bytes) -> bytes:
        raise TallyUnavailable("simulated: connection refused before any byte was sent")


def test_masters_cache_failure_does_not_block_or_fake_anything(tally, tmp_path, monkeypatch):
    fake = tally()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(posting_module, "save_masters_cache", boom)
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.POSTED and len(fake.imports) == 1


def test_preview_ready_not_durable_means_nothing_is_sent(tally, tmp_path, monkeypatch):
    fake = tally()
    fail_on(monkeypatch, Phase.PREVIEW_READY)
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems)
    assert fake.imports == [] and out.erp_result() is None


def test_validation_rejection_is_reported_even_when_it_cannot_be_journaled(tally, tmp_path, monkeypatch):
    fake = tally()
    j = make_job(party_ledger="No Such Vendor")
    fail_on(monkeypatch, Phase.VALIDATION_REJECTED)
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.VALIDATION_REJECTED and "journal_write_failed" in dict(out.problems)
    assert fake.imports == [] and out.erp_result()["ok"] is False


def test_marker_found_before_sending_blocks_even_if_not_journaled(tally, tmp_path, monkeypatch):
    fake = tally()
    fake.add_voucher(TEST_CO[1], narration=f"x finledger:document_id={NEW_DOC}")
    fail_on(monkeypatch, Phase.RECONCILIATION_REQUIRED)
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    codes = dict(out.problems)
    assert out.state == State.BLOCKED and "marker_already_in_tally" in codes and "journal_write_failed" in codes
    assert fake.imports == []


def test_request_bytes_not_saved_means_nothing_is_sent(tally, tmp_path, monkeypatch):
    fake = tally()
    real = FileJournal.save_bytes

    def save(self, doc, name, data):
        if name.endswith("_request.xml"):
            raise OSError(28, "No space left on device")
        return real(self, doc, name, data)
    monkeypatch.setattr(FileJournal, "save_bytes", save)
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and fake.imports == []


def test_undelivered_send_is_clear_only_when_journaled(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    t = Undelivered(port=fake.port, timeout=2)
    out = post_approved(make_job(), approval(make_job()), s, transport=t)
    assert out.state == State.SEND_NOT_DELIVERED and not out.sent and out.erp_result() is None
    assert phases(s)[-2:] == [Phase.SEND_STARTED.value, Phase.SEND_NOT_DELIVERED.value]
    # the next delivery may send: the journal proves nothing left this machine
    assert post_approved(make_job(), approval(make_job()), s).state == State.POSTED


def test_undelivered_send_that_cannot_be_journaled_stays_blocked(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    fail_on(monkeypatch, Phase.SEND_NOT_DELIVERED)
    out = post_approved(make_job(), approval(make_job()), s, transport=Undelivered(port=fake.port, timeout=2))
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems) and not out.sent
    monkeypatch.undo()
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and fake.imports == []


@pytest.mark.parametrize("mode,phase", [("reject", Phase.TALLY_REJECTED), ("garbage", Phase.OUTCOME_UNCERTAIN),
                                        ("hangup_before_create", Phase.OUTCOME_UNCERTAIN),
                                        ("hangup_after_create", Phase.RECONCILED)])
def test_any_journal_failure_after_sending_is_uncertain_and_never_resent(tally, tmp_path, monkeypatch, mode, phase):
    fake = tally(import_mode=mode)
    s = settings_for(fake, tmp_path)
    fail_on(monkeypatch, phase)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
    assert out.erp_result()["ok"] is False
    monkeypatch.undo()
    fake.import_mode = "create"
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED
    assert len(fake.imports) == 1


def test_reconcile_that_cannot_record_stays_blocked_then_succeeds(tally, tmp_path, monkeypatch):
    fake = tally(import_mode="hangup_after_create")
    s = settings_for(fake, tmp_path)
    fail_on(monkeypatch, Phase.RECONCILED)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    out = reconcile(NEW_DOC, DAY, s)
    assert out.state == State.OUTCOME_UNCERTAIN and "journal_write_failed" in dict(out.problems)
    monkeypatch.undo()
    done = reconcile(NEW_DOC, DAY, s)
    assert done.state == State.RECONCILED and done.erp_id and len(fake.imports) == 1


def test_human_clearance_that_cannot_be_recorded_raises_and_stays_blocked(tally, tmp_path, monkeypatch):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    fail_on(monkeypatch, Phase.HUMAN_CLEARED)
    with pytest.raises(OSError):
        clear_for_resend(NEW_DOC, s, actor="ca@example", note="checked Day Book, nothing imported")
    monkeypatch.undo()
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and len(fake.imports) == 1


# ====================== 3. operator CLI ======================


def run_cli(args, env):
    out = io.StringIO()
    code = main(args, env, out)
    return code, json.loads(out.getvalue())


def _env(fake, **extra):
    env = {"FINLEDGER_TALLY_COMPANY": TEST_CO[0], "FINLEDGER_TALLY_COMPANY_GUID": TEST_CO[1],
           "FINLEDGER_TALLY_PORT": str(fake.port), "FINLEDGER_TALLY_TIMEOUT": "3"}
    env.update(extra)
    return env


def test_cli_valid_config_and_health(tally):
    fake = tally()
    assert run_cli(["config"], _env(fake))[0] == 0
    code, body = run_cli(["health"], _env(fake))
    assert code == 0 and body["health"]["masters"] == "ok" and fake.imports == []


def test_cli_invalid_config(tally):
    fake = tally()
    for bad in ({"FINLEDGER_TALLY_PORT": "9000x"}, {"FINLEDGER_TALLY_COMPANY_GUID": "7a5f42d4"},
                {"FINLEDGER_TALLY_TIMEOUT": "-1"}):
        code, body = run_cli(["health"], _env(fake, **bad))
        assert code == 1 and body["ok"] is False and fake.kinds == []
    # a mistyped posting flag fails closed (documented): posting stays off and `config` shows it
    code, body = run_cli(["config"], _env(fake, FINLEDGER_TALLY_POSTING="Enabled"))
    assert code == 0 and body["settings"]["posting_enabled"] is False


def test_cli_tally_unavailable(tally):
    fake = tally()
    fake.down = True
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["status"] == "tally_unreachable"


@pytest.mark.parametrize("reply", [b"<ENVELOPE><BODY>", b"<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER></ENVELOPE>"])
def test_cli_malformed_or_rejected_reply(tally, reply):
    fake = tally()
    fake.company_reply = reply
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["status"] == "malformed_reply"


def test_cli_wrong_company(tally):
    fake = tally(companies=[(TEST_CO[0], OTHER_CO[1])])
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["status"] == "company_guid_mismatch"
    assert set(fake.kinds) == {"Company"}  # never pinned


@pytest.mark.parametrize("companies,status", [([OTHER_CO], "company_unresolved"), ([], "no_company_loaded")])
def test_cli_missing_company(tally, companies, status):
    fake = tally(companies=companies)
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["status"] == status


def test_cli_masters_failure_is_not_ok(tally):
    fake = tally(ledgers={})
    fake.ledgers = {TEST_CO[1]: ["<not-a-ledger"]}
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["masters"].startswith("failed")


def test_cli_unexpected_exception_prints_only_its_type(tally, monkeypatch):
    fake = tally()

    def boom(self, **kw):
        raise KeyError("Secret Traders Pvt Ltd")
    monkeypatch.setattr(TallyConnector, "health", boom)
    out = io.StringIO()
    assert main(["health"], _env(fake), out) == 3
    assert "Secret Traders" not in out.getvalue() and "KeyError" in out.getvalue()


@pytest.mark.parametrize("args", [["post"], ["run"], ["health", "--force"], ["--post"], ["health", "--no-approval"],
                                  ["config", "--enable-posting"]])
def test_cli_has_no_post_command_or_bypass_flag(args):
    out = io.StringIO()
    assert main(args, {}, out) == 64


# ====================== 4. agent shutdown in each phase ======================


class StopSource(Source):
    """Sets ``stop`` while handing over a job, like a service stop arriving mid-tick."""

    def __init__(self, jobs, host_ref):
        super().__init__(jobs)
        self.host_ref = host_ref

    def claim(self):
        self.host_ref[0].stop.set()
        return super().claim()


def _stop_during(monkeypatch, host, target, attr):
    real = getattr(target, attr)

    def wrapped(*a, **k):
        host.stop.set()
        return real(*a, **k)
    monkeypatch.setattr(target, attr, wrapped)


def test_shutdown_while_idle(tally, tmp_path):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([]), Sink())
    threading.Timer(0.1, host.stop.set).start()
    results = host.run(interval=60)
    assert [r.claimed for r in results] == [False] and fake.imports == []
    AgentHost(settings_for(fake, tmp_path), Source([]), Sink()).run(interval=0.01, max_ticks=1)  # lock released


def test_shutdown_during_a_failing_health_check(tally, tmp_path, monkeypatch):
    fake = tally()
    src = Source([claimed()])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())

    def failing(**kw):
        host.stop.set()
        raise TimeoutError("simulated")
    monkeypatch.setattr(host.connector, "health", failing)
    assert host.run(interval=0.01) == [] and src.claims == 0 and host.status()["last_error"]


def test_shutdown_during_job_validation(tally, tmp_path, monkeypatch):
    fake = tally()
    ref = [None]
    host = AgentHost(settings_for(fake, tmp_path), StopSource([claimed(1, party_ledger="No Such Vendor"),
                                                               claimed(2, doc=OTHER_DOC)], ref), Sink())
    ref[0] = host
    results = host.run(interval=0.01)
    assert [r.outcome.state for r in results] == [State.VALIDATION_REJECTED] and fake.imports == []


def test_shutdown_during_pre_send_persistence(tally, tmp_path, monkeypatch):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed(1), claimed(2, doc=OTHER_DOC)]), Sink())
    _stop_during(monkeypatch, host, FileJournal, "save_bytes")
    results = host.run(interval=0.01)
    assert len(results) == 1 and results[0].outcome.state == State.POSTED and len(fake.imports) == 1


def test_shutdown_during_an_uncertain_submission(tally, tmp_path, monkeypatch):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    host = AgentHost(s, Source([claimed(1), claimed(1)]), Sink())
    _stop_during(monkeypatch, host, TallyTransport, "import_vouchers")
    results = host.run(interval=0.01)
    assert [r.outcome.state for r in results] == [State.OUTCOME_UNCERTAIN] and len(fake.imports) == 1
    restarted = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert restarted[0].outcome.state == State.BLOCKED and len(fake.imports) == 1


def test_shutdown_during_post_send_persistence(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    host = AgentHost(s, Source([claimed(1)]), Sink())
    real = FileJournal.append

    def append(self, doc, ph, **kw):
        if ph is Phase.RESPONSE_RECEIVED:
            host.stop.set()
            raise OSError(5, "simulated power loss while recording the reply")
        return real(self, doc, ph, **kw)
    monkeypatch.setattr(FileJournal, "append", append)
    results = host.run(interval=0.01)
    assert results[0].outcome.state == State.OUTCOME_UNCERTAIN and results[0].outcome.erp_id is None
    monkeypatch.undo()
    again = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert again[0].outcome.state == State.BLOCKED and len(fake.imports) == 1


def test_shutdown_during_reconciliation_leaves_the_document_blocked(tally, tmp_path, monkeypatch):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    fail_on(monkeypatch, Phase.RECONCILED)  # the process dies before the reconciliation is recorded
    fake.add_voucher(TEST_CO[1], narration=f"finledger:document_id={NEW_DOC}", amount="1180.00")
    assert reconcile(NEW_DOC, DAY, s).state == State.OUTCOME_UNCERTAIN
    monkeypatch.undo()
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED and len(fake.imports) == 1
    assert reconcile(NEW_DOC, DAY, s).state == State.RECONCILED


def test_a_failing_job_source_does_not_stop_or_crash_the_loop(tally, tmp_path):
    fake = tally()

    class Broken:
        calls = 0

        def claim(self):
            Broken.calls += 1
            raise ConnectionError("platform unreachable: " + "x" * 5000)
    host = AgentHost(settings_for(fake, tmp_path), Broken(), Sink())
    results = host.run(interval=0.01, max_ticks=3)
    assert len(results) == 3 and all(r.error.startswith("claim failed: ConnectionError") for r in results)
    assert all(len(r.error) <= 300 for r in results) and host.status()["last_error"] == "claim failed: ConnectionError"


def test_a_source_returning_something_else_posts_nothing(tally, tmp_path):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([make_job()]), Sink())
    host.startup()
    r = host.tick()
    assert r.error == "invalid claim: dict" and r.outcome is None and fake.imports == []


# ====================== 5. bounded, PII-free logs ======================


def test_logs_are_bounded_and_carry_no_ledger_names_or_narration(tally, tmp_path, caplog, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    caplog.set_level(logging.DEBUG, logger="finledger_tally")
    post_approved(make_job(), approval(make_job()), s)  # posted
    fake.import_mode = "reject"
    j = make_job(doc=OTHER_DOC)
    post_approved(j, approval(j), s)  # rejected
    host = AgentHost(s, Source([claimed(5, doc="8c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b")]), Sink())
    host.startup()

    def explode(*a, **k):
        raise RuntimeError("Shree Ganesh Metals " * 200)
    monkeypatch.setattr(host.connector, "post_invoice", explode)
    r = host.tick()
    assert len(r.error) <= 300
    text = [rec.getMessage() for rec in caplog.records]
    assert text and all(len(t) <= 400 for t in text)
    joined = "\n".join(text)
    for secret in ("Shree Ganesh", "Test purchase", "TEST-INV-001", "1180", TEST_CO[0]):
        assert secret not in joined


# ====================== 6. no DTD or entity expansion from a reply ======================

_DTD = (b'<!DOCTYPE r [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY>&b;</BODY></ENVELOPE>")


def test_a_reply_with_a_dtd_is_refused_everywhere(tally, tmp_path):
    from finledger_tally import TallyMalformedResponse, parse_company_list, parse_import_reply
    with pytest.raises(TallyMalformedResponse, match="DTD"):
        parse_company_list(_DTD)
    assert parse_import_reply(_DTD.replace(b"ENVELOPE", b"RESPONSE")).kind == "unclear"
    fake = tally()
    fake.company_reply = _DTD
    code, body = run_cli(["health"], _env(fake))
    assert code == 2 and body["health"]["status"] == "malformed_reply"
