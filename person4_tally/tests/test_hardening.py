"""Persistence failures, offline ordering, an ERP id P3 already holds, and the agent host. SYNTHETIC Tally only."""

from __future__ import annotations

import threading

import pytest

from finledger_tally import (
    AgentHost,
    ClaimedJob,
    FileJournal,
    Phase,
    PostAuthorization,
    State,
    post_approved,
)
from finledger_tally import posting as posting_module
from test_posting import NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 (fixture)

_real_append = FileJournal.append


def fail_on(monkeypatch, phase: Phase):
    def append(self, doc, ph, **kw):
        if ph is phase:
            raise OSError(28, "No space left on device")
        return _real_append(self, doc, ph, **kw)
    monkeypatch.setattr(FileJournal, "append", append)


# ---------- a journal that cannot be written never becomes success ----------


def test_send_started_not_durable_means_nothing_is_sent(tally, tmp_path, monkeypatch):
    fake = tally()
    fail_on(monkeypatch, Phase.SEND_STARTED)
    out = post_approved(make_job(), approval(make_job()), settings_for(fake, tmp_path))
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems)
    assert fake.imports == [] and not out.sent and out.erp_result() is None


@pytest.mark.parametrize("phase", [Phase.RESPONSE_RECEIVED, Phase.POSTED])
def test_a_journal_failure_after_sending_is_uncertain_and_blocks_a_resend(tally, tmp_path, monkeypatch, phase):
    fake = tally()
    s = settings_for(fake, tmp_path)
    fail_on(monkeypatch, phase)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent and out.erp_id is None
    assert out.erp_result()["ok"] is False and "will not resend" in out.erp_result()["error"]
    monkeypatch.undo()
    again = post_approved(make_job(), approval(make_job()), s)
    assert again.state == State.BLOCKED and len(fake.imports) == 1
    assert Phase.POSTED.value not in phases(s)


def test_a_response_file_that_cannot_be_saved_is_uncertain(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    real = FileJournal.save_bytes

    def save(self, doc, name, data):
        if name.endswith("_response.xml"):
            raise OSError(5, "I/O error")
        return real(self, doc, name, data)
    monkeypatch.setattr(FileJournal, "save_bytes", save)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.OUTCOME_UNCERTAIN and out.sent
    assert phases(s)[-1] == Phase.SEND_STARTED.value


def test_saved_request_bytes_may_be_rewritten_only_identically(tmp_path):
    j = FileJournal(tmp_path)
    j.save_bytes(NEW_DOC, "0001_request.xml", b"<A/>")
    assert j.save_bytes(NEW_DOC, "0001_request.xml", b"<A/>")[0].read_bytes() == b"<A/>"
    with pytest.raises(FileExistsError):
        j.save_bytes(NEW_DOC, "0001_request.xml", b"<B/>")


# ---------- offline: the block is recorded before the file exists ----------


def _cache_then_down(fake, s):
    other = make_job(doc="7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b")
    assert post_approved(other, approval(other), s).state == State.POSTED
    fake.down = True


def test_offline_file_write_failure_still_blocks_live_posting(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path, offline_xml_enabled=True)
    _cache_then_down(fake, s)

    def boom(*a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(posting_module, "write_offline_xml", boom)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "artifact_write_failed" in dict(out.problems) and out.artifact is None
    assert phases(s) == [Phase.OFFLINE_ARTIFACT_ISSUED.value]
    fake.down = False
    imports = len(fake.imports)
    assert post_approved(make_job(), approval(make_job()), s).state == State.BLOCKED
    assert len(fake.imports) == imports


def test_offline_journal_failure_writes_no_file(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path, offline_xml_enabled=True)
    _cache_then_down(fake, s)
    fail_on(monkeypatch, Phase.OFFLINE_ARTIFACT_ISSUED)
    out = post_approved(make_job(), approval(make_job()), s)
    assert out.state == State.BLOCKED and "journal_write_failed" in dict(out.problems)
    assert not (s.offline_dir / NEW_DOC).exists()


# ---------- P3 already holds the ERP id: return it, never import again ----------


def test_an_erp_id_recorded_by_p3_is_returned_without_any_tally_call(tally, tmp_path):
    fake = tally()
    j = make_job()
    auth = approval(j, document_status="posted", attempt_result="posted", attempt_erp_id="g-1")
    out = post_approved(j, auth, settings_for(fake, tmp_path))
    assert out.state == State.ALREADY_POSTED and out.erp_id == "g-1" and fake.kinds == []
    assert out.erp_result() == {"ok": True, "erp_id": "g-1", "error": None}


@pytest.mark.parametrize("changes", [{"attempt_erp_id": None}, {"attempt_request_hash": "0" * 64}, {"approvals": None},
                                     {"attempt_revision": 2}, {"client_id": "55555555-5555-4555-8555-555555555555"}])
def test_a_posted_attempt_that_is_not_this_job_is_refused(tally, tmp_path, changes):
    fake = tally()
    j = make_job()
    fields = dict(document_status="posted", attempt_result="posted", attempt_erp_id="g-1")
    fields.update(changes)
    out = post_approved(j, approval(j, **fields), settings_for(fake, tmp_path))
    assert out.state == State.NOT_AUTHORIZED and fake.kinds == []


def test_authorization_from_p2_rows():
    auth = PostAuthorization.from_rows("c", {"id": "d", "client_id": "c", "status": "approved"}, {
        "client_id": "c", "document_id": "d", "result": "pending", "revision": 2, "idempotency_key": "d:r2",
        "request_hash": "h", "erp_id": None},
        [{"client_id": "c", "document_id": "d", "kind": "human", "revision": 2, "superseded_at": None},
         {"client_id": "c", "document_id": "d", "kind": "payment_release", "revision": 1, "superseded_at": None}])
    assert (auth.client_id, auth.document_id, auth.attempt_revision, auth.attempt_erp_id) == ("c", "d", 2, None)
    assert auth.approvals == (("human", 2), ("payment_release", 1))


# ---------- agent host ----------


class Source:
    def __init__(self, jobs):
        self.jobs, self.claims = list(jobs), 0

    def claim(self):
        self.claims += 1
        return self.jobs.pop(0) if self.jobs else None


class Sink:
    def __init__(self, fail=False):
        self.reports, self.fail = [], fail

    def report(self, claimed, result):
        if self.fail:
            raise ConnectionError("platform unreachable")
        self.reports.append((claimed.job_id, dict(result)))


def claimed(job_id=1, **job_kw):
    j = make_job(**job_kw)
    return ClaimedJob(job_id, j, approval(j))


def test_agent_does_not_claim_when_posting_is_disabled(tally, tmp_path):
    fake = tally()
    src = Source([claimed()])
    host = AgentHost(settings_for(fake, tmp_path, posting_enabled=False), src, Sink())
    assert host.run(interval=0.01, max_ticks=3) == [] and src.claims == 0
    assert host.startup_report.posting == "disabled" and fake.imports == []


def test_agent_does_not_claim_when_the_company_is_not_verified(tally, tmp_path):
    fake = tally()
    src = Source([claimed()])
    s = settings_for(fake, tmp_path, company_guid="0b1c2d3e-4f50-4a61-8b72-c83d94e5f699")
    assert AgentHost(s, src, Sink()).run(interval=0.01, max_ticks=3) == [] and src.claims == 0


def test_agent_posts_once_and_reports(tally, tmp_path):
    fake = tally()
    sink = Sink()
    results = AgentHost(settings_for(fake, tmp_path), Source([claimed(7)]), sink).run(interval=0.01, max_ticks=2)
    assert [r.claimed for r in results] == [True, False]
    assert results[0].outcome.state == State.POSTED and len(fake.imports) == 1
    assert sink.reports[0][0] == 7 and sink.reports[0][1]["ok"] is True


def test_agent_reports_nothing_when_tally_goes_down_and_does_not_retry(tally, tmp_path):
    fake = tally()
    sink = Sink()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed()]), sink)
    host.startup()
    fake.down = True
    r = host.tick()
    assert r.outcome.state == State.TALLY_UNAVAILABLE and not r.reported and sink.reports == []
    assert fake.imports == []


def test_agent_survives_a_failed_report_and_the_journal_still_blocks(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    c = claimed()
    host = AgentHost(s, Source([c, c]), Sink(fail=True))
    host.startup()
    first, second = host.tick(), host.tick()
    assert first.outcome.state == State.POSTED and first.error.startswith("report failed")
    assert second.outcome.state == State.ALREADY_POSTED and len(fake.imports) == 1


def test_agent_survives_an_unexpected_error(tally, tmp_path, monkeypatch):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed(1), claimed(2, doc=NEW_DOC)]), Sink())
    host.startup()
    calls = []

    def explode(*a, **k):
        calls.append(1)
        raise RuntimeError("bug")
    monkeypatch.setattr(host.connector, "post_invoice", explode)
    r = host.tick()
    assert r.claimed and r.error == "RuntimeError: bug" and r.outcome is None


def test_agent_stops_promptly(tally, tmp_path):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([]), Sink())
    stopper = threading.Timer(0.2, host.stop.set)
    stopper.start()
    results = host.run(interval=60)  # would wait a minute without the stop event
    stopper.join()
    assert len(results) == 1


def test_agent_interval_must_be_positive(tally, tmp_path):
    fake = tally()
    with pytest.raises(ValueError):
        AgentHost(settings_for(fake, tmp_path), Source([]), Sink()).run(interval=0)
