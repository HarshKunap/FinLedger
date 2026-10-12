"""The live-Import preflight and its gated executor, against a SYNTHETIC Tally only. No test here (or anywhere) calls
the executor against a real Tally; the procedure itself has never been run (docs/LIVE_IMPORT_TEST_PROCEDURE.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fake_tally import FakeTally
from finledger_tally import State
from finledger_tally.preflight import (
    LIVE_TEST_NAME_PREFIX,
    confirmation_phrase,
    execute_live_import,
    live_import_preflight,
)
from test_masters import ledger
from test_posting import approval, make_job, settings_for
from test_voucher_xml import PURCHASE, VENDOR

LIVE = (f"{LIVE_TEST_NAME_PREFIX} 20261011", "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b")
OTHER = ("Client Books Pvt Ltd", "0b1c2d3e-4f50-4a61-8b72-c83d94e5f601")
SRC = Path(__file__).resolve().parents[1] / "src" / "finledger_tally"


def live_tally(**kw):
    kw.setdefault("companies", [LIVE])
    kw.setdefault("ledgers", {LIVE[1]: [ledger(VENDOR, "Sundry Creditors", LIVE[1], n=1),
                                        ledger(PURCHASE, "Purchase Accounts", LIVE[1], n=2)]})
    kw.setdefault("vouchers", {LIVE[1]: []})
    return FakeTally(**kw)


@pytest.fixture
def fake():
    fakes = []

    def start(**kw):
        f = live_tally(**kw)
        fakes.append(f)
        return f
    yield start
    for f in fakes:
        f.close()


def live_settings(f, tmp_path, **changes):
    fields = dict(company_name=LIVE[0], company_guid=LIVE[1], posting_enabled=False)
    fields.update(changes)
    return settings_for(f, tmp_path, **fields)


def failed(report):
    return {c.name for c in report.failed}


def test_a_ready_preflight_reads_only_and_binds_the_confirmation_to_the_digest(fake, tmp_path):
    f = fake()
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert report.ready, report.summary()
    assert f.imports == [] and "import" not in f.kinds
    assert report.confirmation == confirmation_phrase(LIVE[1], j["document_id"], report.request_sha256)
    text = report.summary()
    assert "READY" in text and report.confirmation in text and report.request_sha256 in text
    assert VENDOR not in text and PURCHASE not in text  # identifiers and digest only


@pytest.mark.parametrize("change,check", [
    ({"posting_enabled": True}, "posting_state"),  # the dry preflight requires posting still off
    ({"company_guid": OTHER[1]}, "disposable_company_designated"),
])
def test_settings_that_fail_the_preflight(fake, tmp_path, change, check):
    f = fake()
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path, **change),
                                   disposable_company_guid=LIVE[1])
    assert not report.ready and check in failed(report) and report.confirmation is None and f.imports == []


@pytest.mark.parametrize("designated", [None, "", "not-a-guid", OTHER[1], LIVE[1].upper()])
def test_the_disposable_company_must_be_designated_exactly(fake, tmp_path, designated):
    f = fake()
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=designated)
    assert "disposable_company_designated" in failed(report) and not report.ready


def test_a_company_not_named_for_the_live_test_is_refused(fake, tmp_path):
    f = fake(companies=[("FinLedger Test Company", LIVE[1])])
    j = make_job()
    s = live_settings(f, tmp_path, company_name="FinLedger Test Company")
    report = live_import_preflight(j, approval(j), s, disposable_company_guid=LIVE[1])
    assert "company_name_convention" in failed(report) and not report.ready


def test_a_second_loaded_company_stops_before_any_pinned_request(fake, tmp_path):
    f = fake(companies=[LIVE, OTHER])
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert "single_company_loaded" in failed(report) and set(f.kinds) == {"Company"}


def test_a_company_that_holds_any_voucher_is_not_disposable(fake, tmp_path):
    f = fake()
    f.add_voucher(LIVE[1], narration="someone's real entry")
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert "company_has_no_vouchers" in failed(report) and not report.ready


def test_a_policy_approval_is_not_enough_for_the_live_test(fake, tmp_path):
    f = fake()
    j = make_job()
    auth = approval(j, approvals=(("policy_clear", j["revision"]),))
    report = live_import_preflight(j, auth, live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert failed(report) == {"human_approval"}


@pytest.mark.parametrize("changes,code", [({"attempt_request_hash": "0" * 64}, "job_changed"),
                                          ({"approvals": ()}, "approval_missing"),
                                          ({"document_status": "in_review"}, "not_approved")])
def test_authorization_problems_fail_the_preflight(fake, tmp_path, changes, code):
    f = fake()
    j = make_job()
    report = live_import_preflight(j, approval(j, **changes), live_settings(f, tmp_path),
                                   disposable_company_guid=LIVE[1])
    check = next(c for c in report.checks if c.name == "authorization")
    assert not check.ok and code in check.detail


def test_a_document_with_any_journal_history_is_not_fresh(fake, tmp_path):
    f = fake()
    j = make_job()
    s = live_settings(f, tmp_path)
    from finledger_tally import FileJournal, Phase
    FileJournal(s.journal_dir).append(j["document_id"], Phase.PREVIEW_READY, revision=1,
                                      idempotency_key=j["idempotency_key"], job_hash="h")
    report = live_import_preflight(j, approval(j), s, disposable_company_guid=LIVE[1])
    assert "fresh_document" in failed(report)


def test_tally_down_is_not_ready(fake, tmp_path):
    f = fake()
    f.down = True
    j = make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert "tally_reachable" in failed(report) and not report.ready


def test_an_unsupported_draft_is_not_ready(fake, tmp_path):
    f = fake()
    j = make_job(gst={"treatment": "intra", "input_ledgers": ["CGST"], "tax_breakup": {"cgst": "90"}})
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert "preview" in failed(report) and f.imports == []


# ---------- the executor: one send, only after a matching confirmation ----------


def _ready(f, tmp_path, j=None):
    j = j or make_job()
    report = live_import_preflight(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1])
    assert report.ready, report.summary()
    return j, report


def test_execute_refuses_while_posting_is_disabled(fake, tmp_path):
    f = fake()
    j, report = _ready(f, tmp_path)
    again, out = execute_live_import(j, approval(j), live_settings(f, tmp_path), disposable_company_guid=LIVE[1],
                                     confirmation=report.confirmation)
    assert out is None and "posting_state" in failed(again) and f.imports == []


@pytest.mark.parametrize("typed", ["", "yes", None])
def test_execute_refuses_without_the_exact_confirmation(fake, tmp_path, typed):
    f = fake()
    j, report = _ready(f, tmp_path)
    again, out = execute_live_import(j, approval(j), live_settings(f, tmp_path, posting_enabled=True),
                                     disposable_company_guid=LIVE[1], confirmation=typed)
    assert out is None and "confirmation" in failed(again) and f.imports == []


def test_a_confirmation_for_other_bytes_is_refused(fake, tmp_path):
    f = fake()
    _, report = _ready(f, tmp_path)
    changed = make_job(lines=[{"ledger": PURCHASE, "amount": "1181.00", "is_debit": True, "cost_centre": None}],
                       bill_wise={"ref_type": "new_ref", "ref": "TEST-INV-001", "due_date": None})
    final, out = execute_live_import(changed, approval(changed), live_settings(f, tmp_path, posting_enabled=True),
                                     disposable_company_guid=LIVE[1], confirmation=report.confirmation)
    assert out is None and failed(final) == {"confirmation"} and f.imports == []  # all else passed: only the bytes differ


def test_execute_sends_exactly_once_and_never_again(fake, tmp_path):
    f = fake()
    j, report = _ready(f, tmp_path)
    s = live_settings(f, tmp_path, posting_enabled=True)
    final, out = execute_live_import(j, approval(j), s, disposable_company_guid=LIVE[1],
                                     confirmation=report.confirmation)
    assert final.ready and out.state == State.POSTED and len(f.imports) == 1
    assert out.request_sha256 == report.request_sha256  # the confirmed bytes are the bytes sent
    second, out2 = execute_live_import(j, approval(j), s, disposable_company_guid=LIVE[1],
                                       confirmation=report.confirmation)
    assert out2 is None and {"fresh_document", "company_has_no_vouchers"} <= failed(second) and len(f.imports) == 1


def test_an_uncertain_live_send_is_returned_as_uncertain_and_not_retried(fake, tmp_path):
    f = fake(import_mode="hangup_before_create")
    j, report = _ready(f, tmp_path)
    s = live_settings(f, tmp_path, posting_enabled=True)
    _, out = execute_live_import(j, approval(j), s, disposable_company_guid=LIVE[1], confirmation=report.confirmation)
    assert out.state == State.OUTCOME_UNCERTAIN and len(f.imports) == 1
    f.import_mode = "create"
    _, again = execute_live_import(j, approval(j), s, disposable_company_guid=LIVE[1],
                                   confirmation=report.confirmation)
    assert again is None and len(f.imports) == 1


def test_nothing_in_the_package_calls_the_executor_or_exposes_it_on_the_cli():
    callers = [p.name for p in SRC.glob("*.py")
               if p.name != "preflight.py" and re.search(r"execute_live_import\s*\(", p.read_text())]
    assert callers == []
    main = (SRC / "__main__.py").read_text()
    assert "execute_live_import" not in main


def test_the_preflight_function_has_no_import_path():
    src = (SRC / "preflight.py").read_text()
    body = src[src.index("def live_import_preflight"):src.index("def _all_vouchers")]
    assert "import_vouchers" not in body and "post_approved" not in body


def test_execute_binds_the_send_to_the_confirmed_digest(fake, tmp_path, monkeypatch):
    import finledger_tally.preflight as pf
    seen = []
    real = pf.post_approved
    monkeypatch.setattr(pf, "post_approved", lambda *a, **k: seen.append(k) or real(*a, **k))
    f = fake()
    j, report = _ready(f, tmp_path)
    execute_live_import(j, approval(j), live_settings(f, tmp_path, posting_enabled=True),
                        disposable_company_guid=LIVE[1], confirmation=report.confirmation)
    assert [k.get("expected_request_sha256") for k in seen] == [report.request_sha256] and len(f.imports) == 1
