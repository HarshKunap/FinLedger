"""Person 3 -> Person 4 hand-off on a REAL Postgres with person2's migrations and person3's ControlService.

Proves against the real rows (not a description of them) that: P3's approved post job passes P4's authorization
gate, P3's request_hash formula matches P4's, an edited draft is refused, and P4's outcome feeds P3's erp_result.
Tally is the synthetic fake. Skips unless Postgres, psycopg and FastAPI are installed and the run is not root (initdb
refuses root; run as the postgres user, see README).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("fastapi")
EXTERNAL_DSN = os.environ.get("FINLEDGER_TEST_OWNER_DSN")  # person2's convention: an owner DSN of a scratch cluster
if not EXTERNAL_DSN and hasattr(os, "geteuid") and os.geteuid() == 0:
    pytest.skip("initdb refuses root: set FINLEDGER_TEST_OWNER_DSN or run as the postgres user", allow_module_level=True)
if not EXTERNAL_DSN and not shutil.which("initdb"):
    pytest.skip("Postgres not available (no initdb, no FINLEDGER_TEST_OWNER_DSN)", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "person3_control_ui" / "src"))
_spec = importlib.util.spec_from_file_location("p3_conftest", ROOT / "person3_control_ui" / "tests" / "conftest.py")
p3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p3)
app_dsn, pool, world, conn, document = p3.app_dsn, p3.pool, p3.world, p3.conn, p3.document
if EXTERNAL_DSN:
    @pytest.fixture(scope="session")
    def owner_dsn():
        return EXTERNAL_DSN
else:
    owner_dsn = p3.owner_dsn

from fake_tally import FakeTally  # noqa: E402
from finledger_control.service import ControlService  # noqa: E402
from finledger_platform.db import tenant  # noqa: E402
from finledger_tally import AgentSettings, PostAuthorization, State, post_approved  # noqa: E402
from finledger_tally.posting import check_authorization  # noqa: E402
from test_isolation_check import TEST_CO  # noqa: E402
from test_masters import ledger  # noqa: E402

VENDOR, PURCHASE = "Shree Ganesh Metals", "Purchase - Steel"


def _canonical(doc):
    return {"document_id": doc, "document_type": "purchase_invoice",
            "vendor": {"name": VENDOR, "gstin": "27AAACS1234A1Z2"}, "buyer_gstin": "27AAPFU0939F1ZV",
            "invoice_no": "INV-42", "invoice_date": date.today().isoformat(), "due_date": None, "irn": "x",
            "po_number": "PO-1", "grn_number": None, "taxable": 11800, "cgst": 0, "sgst": 0, "igst": 0, "cess": 0,
            "round_off": 0, "total": 11800,
            "line_items": [{"desc": "steel", "hsn_sac": "7208", "qty": 1, "rate": 11800, "taxable": 11800,
                            "tax_rate": 0, "tax_amount": 0}],
            "bank_upi": None, "confidences": {"invoice_no": .2, "date": .2, "gstin": .99, "total": .99},
            "raw_markdown_path": "x", "page_image_paths": []}


def _draft(doc):
    return {"document_id": doc, "date": date.today().isoformat(), "voucher_type": "purchase", "party_ledger": VENDOR,
            "party_create_proposal": False,
            "lines": [{"ledger": PURCHASE, "amount": 11800, "is_debit": True, "cost_centre": None}],
            "gst": {"treatment": "exempt", "input_ledgers": [], "tax_breakup": {}},
            "tds": {"applicable": False, "section": None, "ledger": None, "amount": 0},
            "bill_wise": {"ref_type": "new_ref", "ref": "INV-42", "due_date": None}, "narration": "",
            "attachment_document_id": doc, "reasons": [], "map_trace_id": "22222222-2222-2222-2222-222222222222"}


def _approved(conn, world, document):
    svc = ControlService(conn)
    cid = world["client"]["id"]
    svc.score(cid, document, _canonical(document), world["clerk"])
    routed = svc.submit_draft(cid, document, _draft(document), world["clerk"])
    svc.approve(cid, document, world["approver"], post=True, expected_revision=routed["revision"])
    job = svc.post_job(cid, document)
    with tenant(conn, cid):
        doc_row = conn.execute("select id,client_id,status from documents where id=%s", (document,)).fetchone()
        attempt = conn.execute("select client_id,document_id,result,revision,idempotency_key,request_hash,erp_id "
                               "from posting_attempts "
                               "where job_id=%s", (job["id"],)).fetchone()
        approvals = conn.execute("select client_id,document_id,kind,revision,superseded_at from approvals "
                                 "where document_id=%s", (document,)).fetchall()
    auth = PostAuthorization.from_rows(cid, doc_row, attempt, approvals)
    assert auth.approvals == (("human", attempt["revision"]),)  # the real P3 approve() row
    return svc, job, auth


@pytest.fixture
def fake():
    f = FakeTally(companies=[TEST_CO], ledgers={TEST_CO[1]: [ledger(VENDOR, "Sundry Creditors", n=1),
                                                             ledger(PURCHASE, "Purchase Accounts", n=2)]},
                  vouchers={TEST_CO[1]: []})
    yield f
    f.close()


def _settings(fake, tmp_path, cid):
    return AgentSettings(client_id=str(cid), company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port,
                         timeout=2, state_dir=tmp_path, posting_enabled=True)


def test_real_p3_approval_passes_the_gate_and_posts_once(conn, world, document, fake, tmp_path):
    svc, job, auth = _approved(conn, world, document)
    cid = world["client"]["id"]
    payload = job["payload"]
    s = _settings(fake, tmp_path, cid)
    assert check_authorization(payload, auth, s) == []  # P3's request_hash == P4's p3_request_hash
    out = post_approved(payload, auth, s, expected_total=Decimal("11800"))
    assert out.state == State.POSTED and len(fake.imports) == 1
    result = svc.erp_result(cid, document, job_id=job["id"], revision=payload["revision"],
                            idempotency_key=payload["idempotency_key"], **out.erp_result())
    assert result["status"] == "posted"
    with tenant(conn, cid):
        row = conn.execute("select result, erp_id from posting_attempts where job_id=%s", (job["id"],)).fetchone()
    assert (row["result"], row["erp_id"]) == ("posted", out.erp_id)
    # P3 now holds the erp_id: a re-delivered job returns it (ARCHITECTURE.md) and Tally is not touched
    with tenant(conn, cid):
        doc_row = conn.execute("select id,client_id,status from documents where id=%s", (document,)).fetchone()
        attempt = conn.execute("select client_id,document_id,result,revision,idempotency_key,request_hash,erp_id "
                               "from posting_attempts "
                               "where job_id=%s", (job["id"],)).fetchone()
        approvals = conn.execute("select client_id,document_id,kind,revision,superseded_at from approvals "
                                 "where document_id=%s", (document,)).fetchall()
    again = PostAuthorization.from_rows(cid, doc_row, attempt, approvals)
    calls = len(fake.kinds)
    redelivered = post_approved(payload, again, s)
    assert (redelivered.state, redelivered.erp_id) == (State.ALREADY_POSTED, out.erp_id)
    assert len(fake.kinds) == calls and len(fake.imports) == 1
    # the same rows with the erp_id missing are refused, still without touching Tally
    bare = PostAuthorization.from_rows(cid, doc_row, dict(attempt, erp_id=None), approvals)
    assert post_approved(payload, bare, s).state == State.NOT_AUTHORIZED and len(fake.kinds) == calls


def test_a_payload_edited_after_p3_approval_is_refused(conn, world, document, fake, tmp_path):
    _, job, auth = _approved(conn, world, document)
    payload = dict(job["payload"])
    payload["voucher_draft"] = dict(payload["voucher_draft"], party_ledger="Someone Else")
    out = post_approved(payload, auth, _settings(fake, tmp_path, world["client"]["id"]))
    assert out.state == State.NOT_AUTHORIZED and "job_changed" in dict(out.problems) and fake.kinds == []


def test_tally_rejection_lands_as_p3_exception(conn, world, document, fake, tmp_path):
    svc, job, auth = _approved(conn, world, document)
    fake.import_mode = "reject"
    payload = job["payload"]
    out = post_approved(payload, auth, _settings(fake, tmp_path, world["client"]["id"]))
    assert out.state == State.TALLY_REJECTED
    result = svc.erp_result(world["client"]["id"], document, job_id=job["id"], revision=payload["revision"],
                            idempotency_key=payload["idempotency_key"], **out.erp_result())
    assert result["status"] == "exception"


# ---------- end to end with the real P3 rows, and the agent host in between ----------


class _RowsSource:
    """TEST-ONLY: reads the real P2/P3 rows for one known post job. Not a production queue (there is no agent route;
    docs/CONTRACT_GAPS.md #10)."""

    def __init__(self, conn, cid, document, job):
        self.conn, self.cid, self.document, self.job, self.handed = conn, cid, document, job, 0

    def claim(self):
        from finledger_tally import ClaimedJob
        if self.handed >= 2:  # the original delivery and one duplicate
            return None
        self.handed += 1
        with tenant(self.conn, self.cid):
            doc_row = self.conn.execute("select id,client_id,status from documents where id=%s",
                                        (self.document,)).fetchone()
            attempt = self.conn.execute("select client_id,document_id,result,revision,idempotency_key,request_hash,"
                                        "erp_id from posting_attempts where job_id=%s", (self.job["id"],)).fetchone()
            approvals = self.conn.execute("select client_id,document_id,kind,revision,superseded_at from approvals "
                                          "where document_id=%s", (self.document,)).fetchall()
        return ClaimedJob(self.job["id"], self.job["payload"],
                          PostAuthorization.from_rows(self.cid, doc_row, attempt, approvals))


class _P3Sink:
    def __init__(self, svc, cid, document):
        self.svc, self.cid, self.document, self.results = svc, cid, document, []

    def report(self, claimed, result):
        p = claimed.payload
        self.results.append(self.svc.erp_result(self.cid, self.document, job_id=claimed.job_id,
                                                revision=p["revision"], idempotency_key=p["idempotency_key"],
                                                **result))


def test_end_to_end_with_real_p3_rows(conn, world, document, fake, tmp_path):
    from finledger_tally import AgentHost
    svc, job, _ = _approved(conn, world, document)
    cid = world["client"]["id"]
    sink = _P3Sink(svc, cid, document)
    host = AgentHost(_settings(fake, tmp_path, cid), _RowsSource(conn, cid, document, job), sink)
    results = host.run(interval=0.01, max_ticks=3)
    states = [r.outcome.state if r.outcome else None for r in results]
    assert states == [State.POSTED, State.ALREADY_POSTED, None] and len(fake.imports) == 1
    assert [r["status"] for r in sink.results] == ["posted", "posted"]
    with tenant(conn, cid):
        row = conn.execute("select result, erp_id from posting_attempts where job_id=%s", (job["id"],)).fetchone()
        status = conn.execute("select status from documents where id=%s", (document,)).fetchone()["status"]
    assert (row["result"], row["erp_id"], status) == ("posted", results[0].outcome.erp_id, "posted")


def test_an_edit_after_approval_supersedes_it_for_p4(conn, world, document, fake, tmp_path):
    """P3 edit_field after approve(post=False): the approvals row is marked superseded; an old payload is refused."""
    svc = ControlService(conn)
    cid = world["client"]["id"]
    svc.score(cid, document, _canonical(document), world["clerk"])
    routed = svc.submit_draft(cid, document, _draft(document), world["clerk"])
    svc.approve(cid, document, world["approver"], post=False, expected_revision=routed["revision"])
    svc.edit_field(cid, document, world["approver"], "narration", "edited", expected_revision=routed["revision"])
    with tenant(conn, cid):
        approvals = conn.execute("select client_id,document_id,kind,revision,superseded_at from approvals "
                                 "where document_id=%s", (document,)).fetchall()
        doc_row = conn.execute("select id,client_id,status from documents where id=%s", (document,)).fetchone()
    old = {"document_id": str(document), "client_id": str(cid), "revision": routed["revision"],
           "idempotency_key": f"{document}:r{routed['revision']}", "voucher_draft": _draft(document)}
    attempt = {"client_id": cid, "document_id": document, "result": "pending", "revision": routed["revision"],
               "idempotency_key": old["idempotency_key"], "request_hash": "0" * 64, "erp_id": None}
    auth = PostAuthorization.from_rows(cid, doc_row, attempt, approvals)
    assert auth.superseded and not any(r == routed["revision"] for _, r in auth.approvals)
    out = post_approved(old, auth, _settings(fake, tmp_path, cid))
    assert out.state == State.NOT_AUTHORIZED and "approval_superseded" in dict(out.problems) and fake.kinds == []


def test_an_edit_after_approve_and_post_cancels_the_job_but_leaves_the_attempt_pending(conn, world, document, fake,
                                                                                      tmp_path):
    """Real P2 (migration 005 allows jobs.state 'cancelled') and P3 edit_field. The posting_attempts row stays
    'pending', so P4 must not rely on it alone: the document status and the superseded approval refuse the old job."""
    svc, job, _ = _approved(conn, world, document)
    cid = world["client"]["id"]
    rev = job["payload"]["revision"]
    svc.edit_field(cid, document, world["approver"], "narration", "edited", expected_revision=rev)
    with tenant(conn, cid):
        state = conn.execute("select state from jobs where id=%s", (job["id"],)).fetchone()["state"]
        doc_row = conn.execute("select id,client_id,status from documents where id=%s", (document,)).fetchone()
        attempt = conn.execute("select client_id,document_id,result,revision,idempotency_key,request_hash,erp_id "
                               "from posting_attempts where job_id=%s", (job["id"],)).fetchone()
        approvals = conn.execute("select client_id,document_id,kind,revision,superseded_at from approvals "
                                 "where document_id=%s", (document,)).fetchall()
    assert (state, attempt["result"], doc_row["status"]) == ("cancelled", "pending", "in_review")
    out = post_approved(job["payload"], PostAuthorization.from_rows(cid, doc_row, attempt, approvals),
                        _settings(fake, tmp_path, cid))
    codes = dict(out.problems)
    assert out.state == State.NOT_AUTHORIZED and {"not_approved", "approval_superseded"} <= set(codes)
    assert fake.kinds == []
