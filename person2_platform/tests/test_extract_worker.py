"""extract worker: settlement, guards, replay, retries/dead letters and lost leases, as the RLS-bound app role."""
from __future__ import annotations

import pytest
from conftest import invoice_pdf, statement_xlsx
from psycopg.types.json import Jsonb
from test_link import drain, jobs, send

from finledger_platform import extract_worker
from finledger_platform.db import tenant
from finledger_platform.extract_worker import run_once

INVOICE = {"invoice_no": "INV-2026-0042", "total": "118000.00"}


class Failure(Exception):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


class Recorder:
    def __init__(self, result=INVOICE, error=None, during=None):
        self.calls, self.result, self.error, self.during = [], result, error, during

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return self.result


def ingested(world, client, conn, store, settings, data=None, name="a.pdf", mime="application/pdf"):
    doc = send(client, world.a["public_token"], (name, data or invoice_pdf(), mime)).json()["received"][0]
    assert drain(conn, store, settings) == ["prepared"]
    return doc["document_id"]


def doc_row(conn, world, doc_id):
    with tenant(conn, world.a["id"]):
        return conn.execute("select status, status_reason from documents where id = %s", (doc_id,)).fetchone()


def maker_id(owner, firm_id):
    return owner.execute("select id from users where firm_id = %s and automation", (firm_id,)).fetchone()[0]


def test_success_settles_status_event_and_score_job_atomically(world, client, conn, store, settings, owner):
    doc_id = ingested(world, client, conn, store, settings)
    fake = Recorder()
    assert run_once(conn, store, fake) == "extracted"
    assert fake.calls[0]["document_class"] == "invoice" and "Grand Total" in fake.calls[0]["markdown"]
    assert doc_row(conn, world, doc_id)["status"] == "extracted"
    maker = maker_id(owner, world.firm1)
    [score] = jobs(conn, world.a["id"], "score")
    assert score["payload"]["canonical_invoice"] == INVOICE and score["payload"]["maker_id"] == str(maker)
    [job] = jobs(conn, world.a["id"], "extract")
    assert job["state"] == "done" and job["last_error"] is None
    with tenant(conn, world.a["id"]):
        event = conn.execute("select * from workflow_events where document_id = %s", (doc_id,)).fetchone()
    assert (event["from_status"], event["to_status"], event["action"], event["actor_id"]) == \
        ("received", "extracted", "extracted", maker)
    assert run_once(conn, store, fake) is None


def test_replayed_job_is_idempotent(world, client, conn, store, settings, owner):
    ingested(world, client, conn, store, settings)
    assert run_once(conn, store, Recorder()) == "extracted"
    owner.execute("update jobs set state = 'queued', run_after = now() where queue = 'extract'")
    again = Recorder()
    assert run_once(conn, store, again) == "skipped"
    assert again.calls == [] and len(jobs(conn, world.a["id"], "score")) == 1


def test_statement_is_an_exception_without_a_model_call(world, client, conn, store, settings):
    doc_id = ingested(world, client, conn, store, settings, statement_xlsx(), "statement.xlsx",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with tenant(conn, world.a["id"]):
        assert conn.execute("select document_class from documents where id = %s",
                            (doc_id,)).fetchone()["document_class"] == "statement"
    fake = Recorder()
    assert run_once(conn, store, fake) == "exception"
    assert fake.calls == []
    assert dict(doc_row(conn, world, doc_id)) == {"status": "exception", "status_reason": "statement_not_extracted"}
    assert jobs(conn, world.a["id"], "score") == []


@pytest.mark.parametrize("field,value", [
    ("raw_markdown_path", "00000000-0000-0000-0000-000000000000/x/markdown.md"),
    ("raw_markdown_path", "{client}/../{other}/markdown.md"),
    ("page_image_paths", ["/etc/passwd"]),
    ("client_id", "00000000-0000-0000-0000-000000000000"),
])
def test_foreign_object_keys_are_rejected_before_storage_or_ai(world, client, conn, store, settings, owner,
                                                               field, value, monkeypatch):
    doc_id = ingested(world, client, conn, store, settings)
    if isinstance(value, str):
        value = value.format(client=world.a["id"], other=world.b["id"])
    owner.execute("update jobs set payload = jsonb_set(payload, %s, %s) where queue = 'extract'",
                  ([field], Jsonb(value)))
    monkeypatch.setattr(store, "get", lambda key: pytest.fail("storage read before key check"))
    fake = Recorder()
    assert run_once(conn, store, fake) == "exception"
    assert fake.calls == []
    assert doc_row(conn, world, doc_id)["status_reason"] == "object_key_rejected"


def test_permanent_extract_failure_becomes_document_exception(world, client, conn, store, settings):
    doc_id = ingested(world, client, conn, store, settings)
    assert run_once(conn, store, Recorder(error=Failure("invalid_model_output"))) == "exception"
    assert dict(doc_row(conn, world, doc_id)) == {"status": "exception", "status_reason": "invalid_model_output"}
    assert jobs(conn, world.a["id"], "extract")[0]["state"] == "done"


def test_transient_failures_back_off_dead_letter_and_replay(world, client, conn, store, settings, owner):
    doc_id = ingested(world, client, conn, store, settings)
    owner.execute("update jobs set max_attempts = 2 where queue = 'extract'")
    flaky = Recorder(error=Failure("provider_transient"))
    assert run_once(conn, store, flaky) == "queued"
    [job] = jobs(conn, world.a["id"], "extract")
    assert job["last_error"] == "provider_transient"
    assert owner.execute("select run_after > now() + interval '20 seconds' from jobs where id = %s",
                         (job["id"],)).fetchone()[0]  # backoff
    assert run_once(conn, store, flaky) is None  # not due yet
    owner.execute("update jobs set run_after = now() where queue = 'extract'")
    assert run_once(conn, store, flaky) == "dead"
    assert doc_row(conn, world, doc_id)["status"] == "received"
    assert conn.execute("select requeue_dead_job(%s) as ok", (job["id"],)).fetchone()["ok"]
    assert run_once(conn, store, Recorder()) == "extracted"
    assert len(jobs(conn, world.a["id"], "score")) == 1


def test_raw_exception_text_never_reaches_last_error(world, client, conn, store, settings):
    ingested(world, client, conn, store, settings)
    leak = Recorder(error=RuntimeError("Shree Ganesh Metals GSTIN 27AAACS1234A1Z1 total 118000"))
    assert run_once(conn, store, leak) == "queued"
    assert jobs(conn, world.a["id"], "extract")[0]["last_error"] == "extract processing failed"


def test_lost_lease_rejects_late_result(world, client, conn, store, settings, owner, pool):
    doc_id = ingested(world, client, conn, store, settings)
    with pool.connection() as other:
        def steal():
            owner.execute("update jobs set locked_until = now() - interval '1 second' where queue = 'extract'")
            assert run_once(other, store, Recorder(result={"invoice_no": "WINNER"})) == "extracted"

        assert run_once(conn, store, Recorder(during=steal)) == "lease_lost"
    [score] = jobs(conn, world.a["id"], "score")
    assert score["payload"]["canonical_invoice"] == {"invoice_no": "WINNER"}
    assert doc_row(conn, world, doc_id)["status"] == "extracted"
    with tenant(conn, world.a["id"]):
        assert conn.execute("select count(*) from workflow_events where document_id = %s",
                            (doc_id,)).fetchone()["count"] == 1


def test_lease_expired_while_settling_rolls_back(world, client, conn, store, settings, owner):
    doc_id = ingested(world, client, conn, store, settings)
    expire = lambda: owner.execute("update jobs set claim_token = gen_random_uuid() where queue = 'extract'")
    assert run_once(conn, store, Recorder(during=expire)) == "lease_lost"
    assert doc_row(conn, world, doc_id)["status"] == "received"
    assert jobs(conn, world.a["id"], "score") == []


def test_person1_mock_dispatcher_end_to_end(world, client, conn, store, settings, monkeypatch):
    monkeypatch.setenv("FINLEDGER_LLM_PROVIDER", "mock")
    monkeypatch.delenv("FINLEDGER_ENV", raising=False)
    doc_id = ingested(world, client, conn, store, settings)
    outcome = run_once(conn, store, extract_worker.default_extract())
    row = doc_row(conn, world, doc_id)
    assert outcome == "extracted" and row["status"] == "extracted"
    [score] = jobs(conn, world.a["id"], "score")
    assert score["payload"]["canonical_invoice"]["document_id"] == doc_id


def test_hosted_environment_refuses_mock_provider(monkeypatch):
    monkeypatch.setenv("FINLEDGER_LLM_PROVIDER", "mock")
    monkeypatch.setenv("FINLEDGER_ENV", "staging")
    with pytest.raises(RuntimeError, match="hosted"):
        extract_worker.default_extract()
