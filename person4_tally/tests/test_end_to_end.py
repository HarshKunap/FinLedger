"""End to end, steps 1-16, from a P3-shaped post job to reconciliation, against a SYNTHETIC Tally (fake_tally.py).

Each step is asserted where it happens. The Import replies are assumed shapes (no real Import reply has been captured),
so this proves FinLedger's own sequencing, refusals and journal, not real Tally behaviour. The same flow with the real
person3 ControlService and Postgres rows is ``test_p3_integration.py::test_end_to_end_with_real_p3_rows``.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from fake_tally import FakeTally
from finledger_tally import (
    AgentHost,
    ClaimedJob,
    FileJournal,
    Phase,
    PostAuthorization,
    State,
    TallyConnector,
    post_approved,
    reconcile,
)
from finledger_tally.journal import gate
from finledger_tally.posting import check_authorization, p3_request_hash
from finledger_tally.voucher_xml import DraftRejected, preview_purchase_xml
from test_hardening import Sink, Source, fail_on
from test_isolation_check import TEST_CO
from test_posting import CLIENT, make_job, settings_for

DOCS = {name: f"{n}b1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b" for n, name in enumerate(
    ("ok", "rejected", "timeout", "lost_record"), start=1)}
DAY = date(2026, 4, 1)


def p3_job(doc):
    """What person3 ``_enqueue_post`` writes: the payload, and request_hash over its sorted compact JSON."""
    payload = make_job(doc=doc)
    return payload, p3_request_hash(payload)


def p2_rows(payload, request_hash, *, status="approved", result="pending", erp_id=None):
    doc = payload["document_id"]
    return ({"id": doc, "client_id": CLIENT, "status": status},
            {"client_id": CLIENT, "document_id": doc, "result": result, "revision": payload["revision"],
             "idempotency_key": payload["idempotency_key"], "request_hash": request_hash, "erp_id": erp_id},
            [{"client_id": CLIENT, "document_id": doc, "kind": "human", "revision": payload["revision"],
              "superseded_at": None}])


def claim(job_id, doc, **row_kw):
    payload, h = p3_job(doc)
    return ClaimedJob(job_id, payload, PostAuthorization.from_rows(CLIENT, *p2_rows(payload, h, **row_kw)))


@pytest.fixture
def fake():
    f = FakeTally()
    yield f
    f.close()


def events(s, doc):
    return [e.phase for e in FileJournal(s.journal_dir).events(doc)]


def test_steps_1_to_16(fake, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="finledger_tally")
    s = settings_for(fake, tmp_path)
    connector = TallyConnector(s)

    # 1. draft received: the P3 post job and its hash, as P2 stores them
    payload, request_hash = p3_job(DOCS["ok"])
    assert set(payload) == {"document_id", "client_id", "revision", "idempotency_key", "voucher_draft"}

    # 4. company verification (read-only; nothing else may run before it)
    health = connector.health()
    assert health.status == "company_verified" and health.company_guid == TEST_CO[1] and health.posting == "enabled"

    # 5. ledger mapping: the draft's ledgers exist in the verified company's masters, by exact name
    masters = connector.fetch_masters()
    draft = payload["voucher_draft"]
    assert {draft["party_ledger"], draft["lines"][0]["ledger"]} <= masters.ledgers

    # 2. draft validation: a draft naming a ledger Tally does not have is refused before any send
    company = connector.connect()
    with pytest.raises(DraftRejected) as bad:
        preview_purchase_xml(make_job(doc=DOCS["ok"], party_ledger="Unknown Vendor"), company=company,
                             expected_company_name=s.company_name, masters=masters)
    assert "missing_ledger" in bad.value.codes

    # 6. preview generation: deterministic bytes for the approved job
    preview = preview_purchase_xml(payload, company=company, expected_company_name=s.company_name, masters=masters)
    assert preview == preview_purchase_xml(payload, company=company, expected_company_name=s.company_name,
                                           masters=masters)

    # 3. approval lookup: the P2 rows for this exact revision
    auth = PostAuthorization.from_rows(CLIENT, *p2_rows(payload, request_hash))
    assert auth.approvals == (("human", 1),) and auth.document_id == DOCS["ok"]

    # 7. posting eligibility: authorization clean and the local journal clear
    assert check_authorization(payload, auth, s) == []
    assert gate(FileJournal(s.journal_dir).events(DOCS["ok"])).status == "clear"
    assert fake.imports == []

    # 8-10. send_started is durable before the Import; the bytes sent are the preview; success is verified by lookup
    sink = Sink()
    host = AgentHost(s, Source([ClaimedJob(101, payload, auth)]), sink)
    host.startup()
    posted = host.tick().outcome
    assert posted.state == State.POSTED and posted.erp_id.startswith(TEST_CO[1] + "-")
    assert fake.imports == [preview.xml] and posted.request_sha256 == preview.sha256
    phases = events(s, DOCS["ok"])
    assert phases.index(Phase.SEND_STARTED.value) < phases.index(Phase.RESPONSE_RECEIVED.value) < phases.index(
        Phase.POSTED.value)
    saved = sorted(FileJournal(s.journal_dir)._dir(DOCS["ok"]).glob("*_request.xml"))
    assert [p.read_bytes() for p in saved] == [preview.xml]  # the exact request bytes were kept before sending
    assert sink.reports == [(101, {"ok": True, "erp_id": posted.erp_id, "error": None})]

    # 11. Tally rejection: reported as failed, never re-sent for the same revision
    fake.import_mode = "reject"
    rejected = host_tick(s, claim(102, DOCS["rejected"]))
    assert rejected.state == State.TALLY_REJECTED and rejected.erp_result()["ok"] is False
    fake.import_mode = "create"
    assert host_tick(s, claim(102, DOCS["rejected"])).state == State.TALLY_REJECTED
    assert len(fake.imports) == 2

    # 12. timeout after the Import started: uncertain, never "not created", never re-sent
    fake.import_mode, fake.slow_seconds = "slow", 1.5
    timeout_settings = settings_for(fake, tmp_path, timeout=0.5)
    timed_out = post_approved(*_job_and_auth(DOCS["timeout"]), timeout_settings)
    assert timed_out.state == State.OUTCOME_UNCERTAIN and timed_out.sent and timed_out.erp_id is None
    assert "will not resend" in timed_out.erp_result()["error"]
    fake.import_mode = "create"

    # 13. the reply arrives but cannot be recorded: uncertain, not posted
    with monkeypatch.context() as m:
        fail_on(m, Phase.POSTED)
        lost = post_approved(*_job_and_auth(DOCS["lost_record"]), s)
    assert lost.state == State.OUTCOME_UNCERTAIN and lost.erp_id is None
    imports = len(fake.imports)

    # 14. agent restart: a new process on the same state folder re-sends nothing that is uncertain
    restarted = AgentHost(s, Source([claim(103, DOCS["timeout"]), claim(104, DOCS["lost_record"])]), Sink())
    results = restarted.run(interval=0.01, max_ticks=2)
    assert [r.outcome.state for r in results] == [State.BLOCKED, State.BLOCKED] and len(fake.imports) == imports

    # 15. read-only reconciliation: not_found keeps it blocked; a matching voucher settles it; no Import either way
    kinds_before = len(fake.kinds)
    still = reconcile(DOCS["timeout"], DAY, s)
    assert still.state == State.OUTCOME_UNCERTAIN and "not proof" in still.detail
    settled = reconcile(DOCS["lost_record"], DAY, s)  # its voucher was created before the record was lost
    assert settled.state == State.RECONCILED and settled.erp_id
    assert "import" not in fake.kinds[kinds_before:] and len(fake.imports) == imports

    # 16. already-posted duplicate delivery: same voucher id, no Tally Import (local journal and P3's erp_id both)
    again = host_tick(s, ClaimedJob(101, payload, auth))
    assert (again.state, again.erp_id) == (State.ALREADY_POSTED, posted.erp_id)
    recorded = claim(101, DOCS["ok"], status="posted", result="posted", erp_id=posted.erp_id)
    host = AgentHost(s, Source([recorded]), Sink())
    host.startup()  # the health check reads Tally; the duplicate delivery itself must not
    calls = len(fake.kinds)
    assert host.tick().outcome.erp_id == posted.erp_id and len(fake.kinds) == calls
    assert len(fake.imports) == imports

    # nothing in the logs names a ledger, a reference or an amount
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (draft["party_ledger"], draft["lines"][0]["ledger"], "TEST-INV-001", "1180"):
        assert secret not in text


def _job_and_auth(doc):
    payload, h = p3_job(doc)
    return payload, PostAuthorization.from_rows(CLIENT, *p2_rows(payload, h))


def host_tick(s, claimed_job):
    host = AgentHost(s, Source([claimed_job]), Sink())
    host.startup()
    return host.tick().outcome
