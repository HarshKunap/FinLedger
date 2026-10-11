"""Posting state machine against a SYNTHETIC Tally (fake_tally.py). No test talks to a real Tally: every port is a
fresh local test server's. These tests prove FinLedger's own refusal, journal and no-resend behaviour only."""

from __future__ import annotations

import copy
import json
import logging
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    AgentSettings,
    FileJournal,
    Phase,
    PostAuthorization,
    State,
    TallyConnector,
    TallyTransport,
    TallyUnavailable,
    clear_for_resend,
    p3_request_hash,
    parse_import_reply,
    post_approved,
    reconcile,
)
from finledger_tally.posting import load_masters_cache
from test_isolation_check import OTHER_CO, TEST_CO
from test_voucher_xml import DOC, job as base_job

NEW_DOC = "4b1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"
CLIENT = "33333333-3333-4333-8333-333333333333"


def make_job(doc=NEW_DOC, revision=1, **draft_changes):
    j = base_job(**draft_changes)
    j["document_id"] = doc
    j["revision"] = revision
    j["idempotency_key"] = f"{doc}:r{revision}"
    j["voucher_draft"]["document_id"] = doc
    j["voucher_draft"]["attachment_document_id"] = doc
    return json.loads(json.dumps(j))  # as read back from jobs.payload


def approval(j, **changes):
    fields = dict(client_id=CLIENT, document_status="approved", attempt_result="pending",
                  attempt_revision=j["revision"], attempt_idempotency_key=j["idempotency_key"],
                  attempt_request_hash=p3_request_hash(j), approvals=(("human", j["revision"]),),
                  document_id=j["document_id"])
    fields.update(changes)
    return PostAuthorization(**fields)


@pytest.fixture
def tally():
    fakes = []

    def start(**kw):
        fake = FakeTally(**kw)
        fakes.append(fake)
        return fake

    yield start
    for fake in fakes:
        fake.close()


def settings_for(fake, tmp_path, **changes):
    fields = dict(client_id=CLIENT, company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port, timeout=2,
                  state_dir=tmp_path / "state", posting_enabled=True)
    fields.update(changes)
    return AgentSettings(**fields)


def phases(settings, doc=NEW_DOC):
    return [e.phase for e in FileJournal(settings.journal_dir).events(doc)]


# ---------- nothing reaches Tally without authorization ----------


def test_posting_is_disabled_by_default(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path, posting_enabled=False)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.NOT_AUTHORIZED and ("posting_disabled" in dict(out.problems))
    assert fake.kinds == [] and out.erp_result() is None
    assert AgentSettings().posting_enabled is False


@pytest.mark.parametrize("changes,code", [
    ({"document_status": "in_review"}, "not_approved"),
    ({"document_status": "posted"}, "not_approved"),
    ({"document_status": "exception"}, "not_approved"),
    ({"attempt_result": "posted"}, "not_pending"),
    ({"attempt_result": "failed"}, "not_pending"),
    ({"attempt_revision": 2}, "revision_mismatch"),
    ({"attempt_idempotency_key": f"{NEW_DOC}:r2"}, "idempotency_mismatch"),
    ({"client_id": "55555555-5555-4555-8555-555555555555"}, "client_mismatch"),
    ({"attempt_request_hash": "0" * 64}, "job_changed"),
    ({"approvals": None}, "approval_missing"),
    ({"approvals": ()}, "approval_missing"),
    ({"approvals": (("human", 2),)}, "approval_missing"),
    ({"approvals": (("payment_release", 1),)}, "approval_missing"),
    ({"document_id": None}, "document_mismatch"),
    ({"document_id": "7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"}, "document_mismatch"),
])
def test_unapproved_or_mismatched_jobs_never_reach_tally(tally, tmp_path, changes, code):
    fake = tally()
    j = make_job()
    out = post_approved(j, approval(j, **changes), settings_for(fake, tmp_path))
    assert out.state == State.NOT_AUTHORIZED and code in dict(out.problems)
    assert fake.kinds == []


def test_a_draft_edited_after_approval_is_refused(tally, tmp_path):
    fake = tally()
    j = make_job()
    auth = approval(j)
    j["voucher_draft"]["lines"][0]["amount"] = "11800.00"
    out = post_approved(j, auth, settings_for(fake, tmp_path))
    assert "job_changed" in dict(out.problems) and fake.kinds == []


@pytest.mark.parametrize("missing", ["company_guid", "client_id", "company_name"])
def test_an_unpaired_agent_never_reaches_tally(tally, tmp_path, missing):
    fake = tally()
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path, **{missing: None}))
    assert out.state == State.NOT_AUTHORIZED and fake.kinds == []


@pytest.mark.parametrize("auth", [None, {"document_status": "approved"}, "approved"])
def test_no_approval_record_never_reaches_tally(tally, tmp_path, auth):
    fake = tally()
    out = post_approved(make_job(), auth, settings_for(fake, tmp_path))
    assert out.state == State.NOT_AUTHORIZED and fake.kinds == []


# ---------- the happy path, once ----------


def test_approved_job_posts_once_and_is_confirmed_by_lookup(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    j = make_job()
    out = post_approved(j, approval(j), s)
    assert out.state == State.POSTED and out.sent
    assert out.erp_id == f"{TEST_CO[1]}-00000001" and out.master_id == 1 and out.alter_id == 3
    assert len(fake.imports) == 1
    assert out.erp_result() == {"ok": True, "erp_id": out.erp_id, "error": None}
    # the Import sent is exactly the preview the journal recorded, and the reply is stored byte-exact
    folder = s.journal_dir / NEW_DOC
    assert (folder / "0002_request.xml").read_bytes() == fake.imports[0]
    assert (folder / "0003_response.xml").read_bytes() == fake.server.sent[fake.kinds.index("import")]
    assert phases(s) == ["preview_ready", "send_started", "response_received", "posted"]
    # masters cache written for a later offline file
    assert load_masters_cache(s.masters_cache, __import__("finledger_tally").TallyCompany(*TEST_CO))


def test_a_repeat_call_returns_the_same_erp_id_without_touching_tally(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    j = make_job()
    first = post_approved(j, approval(j), s)
    calls = len(fake.kinds)
    again = post_approved(j, approval(j), s)
    assert again.state == State.ALREADY_POSTED and again.erp_id == first.erp_id
    assert len(fake.kinds) == calls and len(fake.imports) == 1


def test_a_new_revision_of_a_posted_document_is_blocked(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    j2 = make_job(revision=2, narration="corrected")
    out = post_approved(j2, approval(j2), s)
    assert out.state == State.BLOCKED and "already_posted_other" in dict(out.problems)
    assert len(fake.imports) == 1 and out.erp_result()["ok"] is False


# ---------- refusals before sending ----------


def test_missing_ledger_is_rejected_and_never_created(tally, tmp_path):
    fake = tally()
    j = make_job(party_ledger="Brand New Vendor")
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.VALIDATION_REJECTED and "missing_ledger" in dict(out.problems)
    assert fake.imports == [] and "import" not in fake.kinds
    assert out.erp_result()["ok"] is False and "validation_rejected" in out.erp_result()["error"]


@pytest.mark.parametrize("draft", [
    {"gst": {"treatment": "intra", "input_ledgers": ["Input CGST", "Input SGST"], "tax_breakup": {"cgst": 90, "sgst": 90}}},
    {"tds": {"applicable": True, "section": "194J", "ledger": "TDS 194J", "amount": "100"}},
    {"voucher_type": "debit_note"},
    {"party_create_proposal": True},
])
def test_unsupported_drafts_never_reach_the_import(tally, tmp_path, draft):
    fake = tally()
    j = make_job(**draft)
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.VALIDATION_REJECTED and fake.imports == []


def test_a_voucher_already_carrying_the_marker_blocks_the_send(tally, tmp_path):
    fake = tally()
    fake.add_voucher(TEST_CO[1], narration=f"typed by hand finledger:document_id={NEW_DOC}")
    j = make_job()
    out = post_approved(j, approval(j), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and "marker_already_in_tally" in dict(out.problems)
    assert fake.imports == [] and out.erp_result()["ok"] is False
    # and it stays blocked: the next call does not even read Tally
    calls = len(fake.kinds)
    assert post_approved(j, approval(j), settings_for(fake, tmp_path)).state == State.BLOCKED
    assert len(fake.kinds) == calls


def test_same_name_different_guid_company_is_refused(tally, tmp_path):
    impostor = (TEST_CO[0], "99999999-8888-4777-8666-555555555555")
    fake = tally(companies=[impostor, OTHER_CO], ledgers={impostor[1]: []})
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and fake.kinds == ["Company"] and fake.imports == []
    assert out.erp_result() is None  # transient: the wrong company is open, nothing to tray


def test_company_not_loaded_is_never_pinned(tally, tmp_path):
    fake = tally(companies=[OTHER_CO])
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and fake.kinds == ["Company"]


def test_tally_down_sends_nothing_and_reports_nothing(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    fake.close()
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.TALLY_UNAVAILABLE and not out.sent and out.erp_result() is None
    assert phases(s) == []


def test_corrupt_journal_blocks_without_reading_tally(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    (s.journal_dir / NEW_DOC).mkdir(parents=True)
    (s.journal_dir / NEW_DOC / "journal.jsonl").write_bytes(b'{"seq": 1, "phase": "send_sta')
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_unreadable" in dict(out.problems) and fake.kinds == []


# ---------- Tally's answer ----------


def test_tally_rejection_is_never_success_and_is_not_resent(tally, tmp_path):
    fake = tally(import_mode="reject")
    s = settings_for(fake, tmp_path)
    j = make_job()
    out = post_approved(j, approval(j), s)
    assert out.state == State.TALLY_REJECTED and out.sent and out.erp_id is None
    assert "Voucher totals do not match" in out.tally_errors[0]
    assert out.erp_result()["ok"] is False and "Voucher totals do not match" in out.erp_result()["error"]
    again = post_approved(j, approval(j), s)
    assert again.state == State.TALLY_REJECTED and len(fake.imports) == 1
    # a corrected, re-approved revision may be sent
    fake.import_mode = "create"
    j2 = make_job(revision=2, narration="fixed")
    assert post_approved(j2, approval(j2), s).state == State.POSTED and len(fake.imports) == 2


@pytest.mark.parametrize("mode", ["hangup_before_create", "garbage", "created_not_stored", "created_wrong_amount"])
def test_unclear_outcomes_are_uncertain_and_never_resent(tally, tmp_path, mode):
    fake = tally(import_mode=mode)
    s = settings_for(fake, tmp_path)
    j = make_job()
    out = post_approved(j, approval(j), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
    assert out.erp_result()["ok"] is False and out.erp_result()["error"].startswith("outcome_uncertain")
    fake.import_mode = "create"
    again = post_approved(j, approval(j), s)
    assert again.state == State.BLOCKED and len(fake.imports) == 1
    j2 = make_job(revision=2)
    assert post_approved(j2, approval(j2), s).state == State.BLOCKED and len(fake.imports) == 1


def test_timeout_after_sending_is_uncertain_and_never_resent(tally, tmp_path):
    fake = tally(import_mode="slow", slow_seconds=1.5)
    s = settings_for(fake, tmp_path, timeout=0.4)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and len(fake.imports) == 1


def test_connection_dropped_after_tally_saved_is_found_by_the_follow_up_lookup(tally, tmp_path):
    fake = tally(import_mode="hangup_after_create")
    s = settings_for(fake, tmp_path)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.RECONCILED and out.erp_id == f"{TEST_CO[1]}-00000001"
    assert out.erp_result()["ok"] is True and len(fake.imports) == 1
    assert phases(s)[-2:] == ["outcome_uncertain", "reconciled"]


def test_connection_refused_at_send_time_is_not_delivered_and_may_be_retried(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)

    def refused(self, body):
        raise TallyUnavailable("refused before connecting")

    monkeypatch.setattr(TallyTransport, "import_vouchers", refused)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.SEND_NOT_DELIVERED and out.erp_result() is None
    monkeypatch.undo()
    assert post_approved(make_job(), approval(make_job()), s).state == State.POSTED and len(fake.imports) == 1


# ---------- reconciliation and human clearance ----------


def test_reconcile_never_clears_on_not_found(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    out = reconcile(NEW_DOC, date(2026, 4, 1), s)
    assert out.state == State.OUTCOME_UNCERTAIN and "not proof" in out.detail
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED


def test_reconcile_identifies_a_voucher_that_appeared_later(tally, tmp_path):
    fake = tally(import_mode="slow", slow_seconds=1.0)
    s = settings_for(fake, tmp_path, timeout=0.3)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    fake.add_voucher(TEST_CO[1], narration=f"Test purchase finledger:document_id={NEW_DOC}")
    import time
    time.sleep(1.0)  # let the stalled fake finish
    out = reconcile(NEW_DOC, date(2026, 4, 1), settings_for(fake, tmp_path))
    assert out.state == State.RECONCILED and out.erp_id == f"{TEST_CO[1]}-00000001"
    again = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert again.state == State.ALREADY_POSTED and len(fake.imports) == 1


def test_reconcile_refuses_a_marker_voucher_that_does_not_match_what_was_sent(tally, tmp_path):
    fake = tally(import_mode="created_wrong_amount")
    s = settings_for(fake, tmp_path)
    assert post_approved(make_job(), approval(make_job()), s).state == State.OUTCOME_UNCERTAIN
    out = reconcile(NEW_DOC, date(2026, 4, 1), s)
    assert out.state == State.OUTCOME_UNCERTAIN and "does not match" in out.detail


def test_reconcile_treats_two_marker_vouchers_as_unresolved(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    for _ in range(2):
        fake.add_voucher(TEST_CO[1], narration=f"x finledger:document_id={NEW_DOC}")
    out = reconcile(NEW_DOC, date(2026, 4, 1), s)
    assert out.state == State.OUTCOME_UNCERTAIN and "ambiguous" in out.detail


def test_a_person_can_clear_an_uncertain_send_and_the_lookup_still_runs(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    with pytest.raises(ValueError):
        clear_for_resend(NEW_DOC, s, actor="", note="checked")
    clear_for_resend(NEW_DOC, s, actor="ca@example", note="Checked Day Book for 1-Apr: no such voucher")
    fake.import_mode = "create"
    assert post_approved(make_job(), approval(make_job()), s).state == State.POSTED and len(fake.imports) == 2


def test_a_posted_document_cannot_be_cleared(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    post_approved(make_job(), approval(make_job()), s)
    with pytest.raises(ValueError):
        clear_for_resend(NEW_DOC, s, actor="ca", note="trying to re-post a posted bill")


# ---------- offline XML ----------


def test_offline_xml_when_tally_is_down_then_live_posting_is_blocked(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path, offline_xml_enabled=True)
    other = make_job(doc="7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b")
    post_approved(other, approval(other), s)  # an earlier live post cached the masters
    fake.down = True
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OFFLINE_XML and not out.sent and out.erp_result() is None
    xml = out.artifact.read_bytes()
    assert ET.fromstring(xml).findtext(".//REFERENCE") == "TEST-INV-001"
    meta = json.loads(out.artifact.with_suffix(".json").read_text())
    assert meta["status"].startswith("NOT IMPORTED") and meta["sha256"] == out.request_sha256
    assert "FinLedger Test Vendor" not in json.dumps(meta) and "1180" not in json.dumps(meta)
    fake.down = False
    imports = len(fake.imports)
    live = post_approved(make_job(), approval(make_job()), s)
    assert live.state == State.BLOCKED and "offline" in live.detail and len(fake.imports) == imports


def test_offline_without_cached_masters_writes_nothing(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path, offline_xml_enabled=True)
    fake.close()
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.TALLY_UNAVAILABLE and out.artifact is None
    assert not s.offline_dir.exists()


# ---------- connector routing, logging ----------


def test_credit_debit_and_payment_are_refused_without_any_call(tally, tmp_path):
    fake = tally()
    c = TallyConnector(settings_for(fake, tmp_path))
    j = make_job(voucher_type="credit_note")
    for out in (c.post_credit_debit(j, approval(j)), c.post_payment(j, approval(j)), c.post_invoice(j, approval(j))):
        assert out.state == State.VALIDATION_REJECTED and "unsupported_voucher_type" in dict(out.problems)
    assert fake.kinds == []


def test_logs_carry_ids_and_digests_only(tally, tmp_path, caplog):
    fake = tally()
    caplog.set_level(logging.DEBUG, logger="finledger_tally")
    post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    text = caplog.text
    assert NEW_DOC in text
    for secret in ("FinLedger Test Vendor", "FinLedger Test Purchase", "1180", "TEST-INV-001", "Test purchase"):
        assert secret not in text


# ---------- Import reply parsing ----------


@pytest.mark.parametrize("raw,kind", [
    (b"<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>", "created"),
    (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA><IMPORTRESULT><CREATED>1</CREATED>"
     b"<ALTERED>0</ALTERED><DELETED>0</DELETED><LASTVCHID>7</LASTVCHID><ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS>"
     b"</IMPORTRESULT></DATA></BODY></ENVELOPE>", "created"),
    ("<RESPONSE><CREATED>1</CREATED><ERRORS>0</ERRORS></RESPONSE>".encode("utf-16"), "created"),
    (b"<RESPONSE><LINEERROR>Ledger 'X' does not exist!</LINEERROR><CREATED>0</CREATED><ERRORS>1</ERRORS></RESPONSE>",
     "rejected"),
    (b"<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>Could not set SVCurrentCompany</LINEERROR>"
     b"</DATA></BODY></ENVELOPE>", "rejected"),
    (b"<RESPONSE><CREATED>1</CREATED></RESPONSE>", "unclear"),  # no ERRORS counter: not a clean create
    (b"<RESPONSE><CREATED>1</CREATED><ALTERED>1</ALTERED><ERRORS>0</ERRORS></RESPONSE>", "unclear"),
    (b"<RESPONSE><CREATED>2</CREATED><ERRORS>0</ERRORS></RESPONSE>", "unclear"),
    (b"<RESPONSE><CREATED>0</CREATED><ALTERED>1</ALTERED><ERRORS>1</ERRORS></RESPONSE>", "unclear"),
    (b"<RESPONSE><CREATED>1</CREATED><CREATED>1</CREATED><ERRORS>0</ERRORS></RESPONSE>", "unclear"),
    (b"<RESPONSE><CREATED>one</CREATED><ERRORS>0</ERRORS></RESPONSE>", "unclear"),
    (b"<RESPONSE><CREATED>1</CREATED><ERRORS>0</ERRORS><LINEERROR>x</LINEERROR></RESPONSE>", "unclear"),
    (b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>", "unclear"),
    (b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER></ENVELOPE>", "unclear"),
    (b"<html>200 OK</html>", "unclear"),
    (b"", "unclear"),
    (b"\xff\xfe\x00", "unclear"),
])
def test_import_reply_classification(raw, kind):
    assert parse_import_reply(raw).kind == kind
