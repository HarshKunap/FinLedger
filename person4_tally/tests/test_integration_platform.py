"""Person 2 -> Person 3 -> Person 4 -> Person 3 -> Person 2, end to end on a REAL Postgres (mission Part 18).

Real here: person2's migrations and Row Level Security (every row is read under ``tenant()`` by an app role, never by
the owner), the ``post`` queue (``claim_job`` / ``finish_job``), ``mapping_table``, ``erp_links``, ``open_bills`` and
``masters_snapshots``, person2's FastAPI ``/agent/*`` routes with an agent token paired through
``/clients/{id}/erp/pair``, and person3's ``ControlService`` (score, submit, approve, edit, ``erp_result``).

Synthetic: Tally (fake_tally.py on 127.0.0.1; nothing here reaches a real Tally and no Import XML leaves the test
process), the Import and Bill reply shapes, and the agent's hand-over: person2 has no agent route to claim post jobs or
report results (docs/CONTRACT_GAPS.md #10), so a TEST-ONLY source reads the rows and a TEST-ONLY sink calls person3's
``erp_result`` directly. Neither is a production queue.

Skips unless psycopg, psycopg_pool, fastapi and httpx are installed and a Postgres is available
(FINLEDGER_TEST_OWNER_DSN, or initdb when not root), like test_p3_integration.py.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import shutil
import socket
import sys
from contextlib import ExitStack
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # fastapi.testclient
EXTERNAL_DSN = os.environ.get("FINLEDGER_TEST_OWNER_DSN")  # person2's convention: an owner DSN of a scratch cluster
if not EXTERNAL_DSN and hasattr(os, "geteuid") and os.geteuid() == 0:
    pytest.skip("initdb refuses root: set FINLEDGER_TEST_OWNER_DSN or run as the postgres user",
                allow_module_level=True)
if not EXTERNAL_DSN and not shutil.which("initdb"):
    pytest.skip("Postgres not available (no initdb, no FINLEDGER_TEST_OWNER_DSN)", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "person3_control_ui" / "src"))
_spec = importlib.util.spec_from_file_location("p3_conftest_platform",
                                               ROOT / "person3_control_ui" / "tests" / "conftest.py")
p3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p3)
world, conn, document = p3.world, p3.conn, p3.document

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from fake_tally import FakeTally, bill  # noqa: E402
from finledger_control.service import Conflict, ControlService  # noqa: E402
from finledger_control.workflow import Action, InvalidTransition, TransitionContext, transition  # noqa: E402
from finledger_platform.api import create_app  # noqa: E402
from finledger_platform.config import Settings  # noqa: E402
from finledger_platform.db import firm, make_pool, migrate, tenant  # noqa: E402
from finledger_platform.store import LocalStore  # noqa: E402
from finledger_platform.tenancy import create_client, create_user  # noqa: E402
from finledger_tally import (  # noqa: E402
    AgentHost,
    AgentSettings,
    ClaimedJob,
    DraftRejected,
    FileJournal,
    Health,
    LedgerStatus,
    MappingTable,
    PostAuthorization,
    PostOutcome,
    State,
    TallyCompany,
    TallyConnector,
    identity_marker,
    p3_request_hash,
    parse_import_reply,
    preview_purchase_xml,
    reconcile,
    resolve_ledger,
    resolve_party,
)
from finledger_tally.journal import gate  # noqa: E402
from finledger_tally.posting import (  # noqa: E402
    POSTABLE_DOCUMENT_STATUSES,
    POSTING_APPROVAL_KINDS,
    check_authorization,
    save_masters_cache,
)
from test_identity_probe_kit import _FakeTally  # noqa: E402
from test_isolation_check import OTHER_CO, TEST_CO  # noqa: E402
from test_masters import ledger  # noqa: E402
from test_p3_integration import PURCHASE, VENDOR, _canonical, _draft, _P3Sink  # noqa: E402
from test_posting import tally  # noqa: E402,F401 (fixture)
from test_release_boundary import Undelivered  # noqa: E402

P4_ROLE, P4_PASSWORD = "fl_p4_platform", "fl_p4_platform_test_pw"
CREDITORS = "Sundry Creditors"
TALLY_PURCHASE = "Steel Purchases"  # the Tally ledger a P2 mapping row points the draft's PURCHASE line at
OTHER_CLIENT = "55555555-5555-4555-8555-555555555555"
UNCERTAIN = "outcome_uncertain (reconciliation required; FinLedger will not resend): "
REAL_MASTERS = Path(__file__).parent / "fixtures" / "tallyprime_7_1_edu_masters_20261011_013427"


# ---------- database, platform app and Tally fixtures ----------


if EXTERNAL_DSN:
    @pytest.fixture(scope="session")
    def owner_dsn():
        return EXTERNAL_DSN
else:
    owner_dsn = p3.owner_dsn


@pytest.fixture(scope="session")
def app_dsn(owner_dsn):
    """Like person3's ``app_dsn``, but a role of its own: a member of ``finledger_app`` (RLS applies, not the owner)
    and of person2's ``post`` worker role, so the test claims the job through person2's real ``claim_job``."""
    migrate(owner_dsn)
    with psycopg.connect(owner_dsn, autocommit=True) as c:
        c.execute(f"drop role if exists {P4_ROLE}")
        c.execute(f"create role {P4_ROLE} login password '{P4_PASSWORD}' in role finledger_app, finledger_worker_post")
    info = conninfo_to_dict(owner_dsn)
    info.update(user=P4_ROLE, password=P4_PASSWORD)
    return make_conninfo(**info)


@pytest.fixture(scope="session")
def pool(app_dsn):
    p = make_pool(app_dsn, min_size=1, max_size=4)
    yield p
    p.close()


@pytest.fixture
def owner(owner_dsn, world):
    """The schema owner, used ONLY to move time forward (a lease expiry); never to read what P4 reads."""
    with psycopg.connect(owner_dsn, autocommit=True, row_factory=dict_row) as c:
        yield c


@pytest.fixture
def dead_conn(app_dsn):
    """A real psycopg connection that has gone away (the platform database restarted)."""
    c = psycopg.connect(app_dsn, autocommit=True, row_factory=dict_row)
    c.close()
    return c


def _p2_settings(app_dsn, owner_dsn, root) -> Settings:
    """person2's Settings as its own tests build them (person2_platform/tests/conftest.py); no ClamAV, local store."""
    return Settings(database_url=app_dsn, owner_database_url=owner_dsn, app_base_url="http://testserver",
                    signing_secret=b"test-signing-secret", enc_key="", store="local", store_root=str(root),
                    s3_bucket="", inbound_domain="inbound.finledger.test",
                    inbound_webhook_secret=b"test-webhook-secret", inbound_authserv_id="mx.finledger.test",
                    virus_scanner="eicar", clamd_host="127.0.0.1", clamd_port=3310,
                    max_upload_bytes=5 * 1024 * 1024, rate_per_token_hour=1000, rate_per_ip_hour=1000,
                    render_page_cap=30)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def p2_app(app_dsn, owner_dsn, pool, world, conn, tmp_path):
    """person2's real FastAPI app on the same database, and a site-agent token paired through its real route by a firm
    admin of the world's firm (as person2_platform/tests/test_storage.py pairs one)."""
    settings = _p2_settings(app_dsn, owner_dsn, tmp_path / "objects")
    store = LocalStore(settings.store_root, settings.signing_secret, settings.app_base_url)
    with TestClient(create_app(settings, pool, store)) as http:
        with firm(conn, world["client"]["firm_id"]):
            _, admin_token = create_user(conn, "p4-admin@sharma.test", firm_admin=True)
        paired = http.post(f"/clients/{world['client']['id']}/erp/pair", headers=_bearer(admin_token),
                           json={"erp_type": "tally", "company_name": TEST_CO[0]})
        assert paired.status_code == 201, paired.text
        yield SimpleNamespace(http=http, agent=_bearer(paired.json()["agent_token"]))


def _book(*rows: tuple[str, str]) -> dict:
    return {TEST_CO[1]: [ledger(name, parent, n=n) for n, (name, parent) in enumerate(rows, start=1)]}


@pytest.fixture
def fake(tally):
    """The paired company alone, with the two ledgers person3's draft names."""
    return tally(companies=[TEST_CO], ledgers=_book((VENDOR, CREDITORS), (PURCHASE, "Purchase Accounts")),
                 vouchers={TEST_CO[1]: []})


class _JournalWatchingTally(FakeTally):
    """A FakeTally that, when an Import arrives, records the phases already on disk in the document's journal: proof
    that ``send_started`` was durable BEFORE the bytes reached Tally (not merely written in the right order later)."""

    def __init__(self, journal_root: Path, document: str, **kw):
        super().__init__(**kw)
        self.journal_root, self.document, self.phases_at_import = journal_root, document, []

    def _import(self, body, root):
        self.phases_at_import.append([e.phase for e in FileJournal(self.journal_root).events(self.document)])
        return super()._import(body, root)


def _settings(fake, tmp_path, cid, **changes) -> AgentSettings:
    fields = dict(client_id=str(cid), company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port, timeout=2,
                  state_dir=tmp_path / "state", posting_enabled=True)
    fields.update(changes)
    return AgentSettings(**fields)


# ---------- the platform side: real rows, read the way an agent route would hand them over ----------


def _approve(conn, world, document):
    """person3 score -> submit_draft -> approve(post=True): real rows and the real queued post job."""
    svc, cid = ControlService(conn), world["client"]["id"]
    svc.score(cid, document, _canonical(document), world["clerk"])
    routed = svc.submit_draft(cid, document, _draft(document), world["clerk"])
    svc.approve(cid, document, world["approver"], post=True, expected_revision=routed["revision"])
    return svc, svc.post_job(cid, document)


def _rows(conn, cid, document, job_id):
    """The rows ``PostAuthorization.from_rows`` needs, read under the client's tenant (RLS applies)."""
    with tenant(conn, cid):
        doc = conn.execute("select id, client_id, status from documents where id=%s", (document,)).fetchone()
        attempt = conn.execute("select client_id, document_id, result, revision, idempotency_key, request_hash, erp_id "
                               "from posting_attempts where job_id=%s", (job_id,)).fetchone()
        approvals = conn.execute("select client_id, document_id, kind, revision, superseded_at, actor_id "
                                 "from approvals where document_id=%s order by id", (document,)).fetchall()
    return doc, attempt, approvals


def _auth(conn, cid, document, job_id) -> PostAuthorization:
    return PostAuthorization.from_rows(cid, *_rows(conn, cid, document, job_id))


def _claim(conn):
    """person2's real claim of the ``post`` queue (a lease; returns None when nothing is claimable)."""
    return conn.execute("select * from claim_job('post', 300)").fetchone()


def _finish(conn, job):
    """person2's real ``finish_job`` for a claim; ``p_permanent`` as CONTRACT_GAPS proposes for every P4 outcome."""
    return conn.execute("select finish_job(%s, %s, true, null, true) as r",
                        (job["id"], job["claim_token"])).fetchone()["r"]


def _report(svc, cid, document, job, outcome: PostOutcome):
    p = job["payload"]
    return svc.erp_result(cid, document, job_id=job["id"], revision=p["revision"],
                          idempotency_key=p["idempotency_key"], **outcome.erp_result())


def _p3(conn, cid, document, job_id) -> SimpleNamespace:
    """What person2/person3 hold for the document and this job's posting attempt."""
    with tenant(conn, cid):
        doc = conn.execute("select status, status_reason from documents where id=%s", (document,)).fetchone()
        attempt = conn.execute("select result, erp_id, error from posting_attempts where job_id=%s",
                               (job_id,)).fetchone()
        events = conn.execute("select action, metadata from workflow_events where document_id=%s and action like "
                              "'erp%%' order by id", (document,)).fetchall()
    return SimpleNamespace(status=doc["status"], reason=doc["status_reason"], result=attempt["result"],
                           erp_id=attempt["erp_id"], error=attempt["error"],
                           events=[(e["action"], e["metadata"]) for e in events])


UNTOUCHED = SimpleNamespace(status="approved", reason=None, result="pending", erp_id=None, error=None, events=[])


class _PlatformSource:
    """TEST-ONLY ``JobSource`` standing in for the missing P2 claim route (CONTRACT_GAPS #10): hands over the real post
    job with the P2/P3 rows read under the client's tenant at claim time, ``deliveries`` times. ``auth`` replays an
    authorization read earlier instead (a platform that never learnt the outcome)."""

    def __init__(self, conn, cid, document, job, *, deliveries=1, auth=None):
        self.conn, self.cid, self.document, self.job = conn, cid, document, job
        self.deliveries, self.auth, self.claims, self.handed = deliveries, auth, 0, 0

    def claim(self):
        self.claims += 1
        if self.handed >= self.deliveries:
            return None
        self.handed += 1
        auth = self.auth or _auth(self.conn, self.cid, self.document, self.job["id"])
        return ClaimedJob(self.job["id"], self.job["payload"], auth)


class _ConnectingSource(_PlatformSource):
    """Opens its own database connection for each claim, as a platform route would per request."""

    def __init__(self, dsn, *args, **kw):
        super().__init__(None, *args, **kw)
        self.dsn = dsn

    def claim(self):
        with psycopg.connect(self.dsn, connect_timeout=2, autocommit=True, row_factory=dict_row) as c:
            self.conn = c
            return super().claim()


class _UndeliveredConnector(TallyConnector):
    """Reads reach the fake; the Import is refused before any byte leaves this machine (connection refused)."""

    @property
    def transport(self):
        return Undelivered(port=self.settings.port, timeout=self.settings.timeout)


def _run_once(settings, source, sink, **kw):
    [tick] = AgentHost(settings, source, sink, **kw).run(interval=0.01, max_ticks=1)
    return tick


# ====================== steps 1-15: a real P2 post job to the ERP id in P2 ======================


def test_steps_1_to_15_from_a_p2_post_job_to_the_erp_id_in_p2(conn, world, document, p2_app, tmp_path):
    cid = world["client"]["id"]
    svc, queued = _approve(conn, world, document)
    fake = _JournalWatchingTally(tmp_path / "state" / "journal", document, companies=[TEST_CO],
                                 vouchers={TEST_CO[1]: []}, ledgers=_book((VENDOR, CREDITORS), (TALLY_PURCHASE, "Purchase Accounts")))
    try:
        _steps_1_to_15(conn, world, document, p2_app, fake, tmp_path, svc, cid, queued)
    finally:
        fake.close()


def _steps_1_to_15(conn, world, document, p2_app, fake, tmp_path, svc, cid, queued):

    # 1. the job arrives: person2's own claim of the 'post' queue hands over person3's job row
    job = _claim(conn)
    assert (job["id"], job["queue"], job["state"], job["attempts"]) == (queued["id"], "post", "running", 1)
    payload = job["payload"]
    assert set(payload) == {"document_id", "client_id", "revision", "idempotency_key", "voucher_draft"}
    assert str(job["document_id"]) == payload["document_id"] == document and payload["client_id"] == str(cid)
    assert _claim(conn) is None  # leased: not handed out a second time while the lease runs

    # 2. client/document scope: rows are read under the client's tenant; another client of the firm sees none of them
    doc_row, attempt, approvals = _rows(conn, cid, document, job["id"])
    with firm(conn, world["client"]["firm_id"]):
        other = create_client(conn, "Other Client", inbound_slug="other-client")
    assert _rows(conn, other["id"], document, job["id"]) == (None, None, [])
    with pytest.raises(ValueError, match="same client"):
        PostAuthorization.from_rows(other["id"], doc_row, attempt, approvals)

    # 3. the approval record: person3's approve() row for this revision, by the approver (not the clerk)
    auth = PostAuthorization.from_rows(cid, doc_row, attempt, approvals)
    assert [(a["kind"], a["actor_id"], a["superseded_at"]) for a in approvals] == [("human", world["approver"], None)]
    assert auth.approvals == (("human", payload["revision"]),) and auth.document_id == document

    # 4. the approved revision: job, attempt and approval agree, and the job hashes to person3's request_hash
    assert payload["revision"] == attempt["revision"] == approvals[0]["revision"] == 1
    assert attempt["request_hash"] == p3_request_hash(payload) and attempt["result"] == "pending"

    # 5. company identity: erp_links holds the paired name; its GUID is read by health() at pairing (gap 3)
    with tenant(conn, cid):
        link = conn.execute("select company_name from erp_links").fetchone()
    pairing = TallyConnector(AgentSettings(company_name=link["company_name"], port=fake.port, timeout=2)).health()
    assert (pairing.status, pairing.company_guid, pairing.posting) == (Health.COMPANY_VERIFIED, TEST_CO[1], "disabled")
    s = _settings(fake, tmp_path, cid, company_name=link["company_name"], company_guid=pairing.company_guid)
    connector = TallyConnector(s)
    company = connector.connect()
    assert company == TallyCompany(*TEST_CO)

    # 6. masters: read from the pinned company, and sent to person2's real /agent/masters route
    masters = connector.fetch_masters()
    assert masters.ledgers == {VENDOR, TALLY_PURCHASE} and masters.company_guid == TEST_CO[1]
    for body in connector.masters_snapshot():
        assert p2_app.http.post("/agent/masters", headers=p2_app.agent, json=body).status_code == 201
    with tenant(conn, cid):
        snap = conn.execute("select items from masters_snapshots where kind='ledger'").fetchone()["items"]
    assert sorted(item["name"] for item in snap) == sorted(masters.ledgers)

    # 7. ledger mapping: P2 mapping_table rows (written through /agent/mapping) map the draft's line to Tally's name
    gstin = _canonical(document)["vendor"]["gstin"]
    for row in ({"kind": "ledger", "our_key": PURCHASE, "erp_value": TALLY_PURCHASE},
                {"kind": "party", "our_key": gstin, "erp_value": VENDOR}):
        assert p2_app.http.put("/agent/mapping", headers=p2_app.agent, json=row).status_code == 200
    with tenant(conn, cid):
        rows = conn.execute("select client_id, kind, our_key, erp_value from mapping_table").fetchall()
        stored_gstin = conn.execute("select payload->'vendor'->>'gstin' as g from canonical_invoices "
                                    "where document_id=%s", (document,)).fetchone()["g"]
    with pytest.raises(ValueError, match="same client"):
        MappingTable.from_rows(rows, client_id=other["id"])
    mapping = MappingTable.from_rows(rows, client_id=cid)
    assert resolve_ledger(PURCHASE, masters, s.company_guid).status is LedgerStatus.MISSING
    resolved = resolve_ledger(PURCHASE, masters, s.company_guid, mapping)
    assert (resolved.status, resolved.tally_name) == (LedgerStatus.EXISTS, TALLY_PURCHASE)
    party = resolve_party(VENDOR, masters, s.company_guid, mapping, vendor_gstin=stored_gstin)
    assert (party.status, party.tally_name) == (LedgerStatus.EXISTS, VENDOR)

    # 8. draft validation: person3's draft passes P4's checks only with the mapping, against P2's invoice total
    with tenant(conn, cid):
        total = Decimal(conn.execute("select payload->>'total' as t from canonical_invoices where document_id=%s",
                                     (document,)).fetchone()["t"])
    build = dict(company=company, expected_company_name=s.company_name, masters=masters)
    with pytest.raises(DraftRejected) as unmapped:
        preview_purchase_xml(payload, **build)
    assert "missing_ledger" in unmapped.value.codes
    with pytest.raises(DraftRejected) as wrong_total:
        preview_purchase_xml(payload, **build, expected_total=total + 1, mapping=mapping)
    assert wrong_total.value.codes == {"amount_mismatch"}

    # 9. preview: deterministic bytes naming Tally's ledger, never the draft's unmapped name
    preview = preview_purchase_xml(payload, **build, expected_total=total, mapping=mapping)
    assert preview == preview_purchase_xml(payload, **build, expected_total=total, mapping=mapping)
    assert (preview.stage, preview.reference, preview.party_amount) == ("preview_ready", "INV-42", total)
    assert [line[0] for line in preview.lines] == [TALLY_PURCHASE] and PURCHASE.encode() not in preview.xml

    # 10. eligibility: the rows authorize exactly this job and the journal is clear; nothing was imported yet
    assert check_authorization(payload, auth, s) == []
    stale = dict(payload, revision=2, idempotency_key=f"{document}:r2")
    assert {"approval_missing", "revision_mismatch", "idempotency_mismatch", "job_changed"} <= {
        code for code, _ in check_authorization(stale, auth, s)}
    journal = FileJournal(s.journal_dir)
    assert Path(s.journal_dir) == fake.journal_root
    assert gate(journal.events(document)).status == "clear" and fake.imports == []

    # 11-13. send_started is durable before the one Import; the Import carries the preview bytes; success is parsed
    out = connector.post_invoice(payload, auth, mapping=mapping, expected_total=total)
    assert out.state == State.POSTED and out.sent and out.erp_id.startswith(TEST_CO[1] + "-")
    assert fake.imports == [preview.xml] and out.request_sha256 == preview.sha256
    assert fake.phases_at_import == [["preview_ready", "send_started"]]  # durable before the Import, not after
    events = journal.events(document)
    assert [e.phase for e in events] == ["preview_ready", "send_started", "response_received", "posted"]
    started = events[1]
    assert (started.request_sha256, started.reference, started.party_amount, started.job_hash) == (
        preview.sha256, "INV-42", f"{total:.2f}", attempt["request_hash"])
    folder = Path(s.journal_dir) / document
    assert [p.read_bytes() for p in sorted(folder.glob("*_request.xml"))] == [preview.xml]
    [response] = sorted(folder.glob("*_response.xml"))
    assert parse_import_reply(response.read_bytes()).kind == "created" and events[-1].erp_id == out.erp_id

    # 14. the ERP identity reaches person2 through person3's erp_result
    assert _report(svc, cid, document, job, out) == {"status": "posted"}

    # 15. person2/person3 hold it: document posted, attempt posted with the voucher GUID; the queue job is done
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id, st.error) == ("posted", "posted", out.erp_id, None)
    assert st.events == [("erp_posted", {"erp_id": out.erp_id, "error": None})]
    assert _finish(conn, job) == "done"
    assert fake.imports == [preview.xml]


# ====================== 16. duplicate delivery ======================


def test_a_post_job_re_leased_by_p2_returns_the_recorded_erp_id_without_any_tally_call(conn, owner, world, document,
                                                                                      fake, tmp_path):
    cid = world["client"]["id"]
    svc, _ = _approve(conn, world, document)
    first = _claim(conn)
    s = _settings(fake, tmp_path, cid)
    out = TallyConnector(s).post_invoice(first["payload"], _auth(conn, cid, document, first["id"]))
    assert out.state == State.POSTED
    assert _report(svc, cid, document, first, out) == {"status": "posted"}  # then the agent dies before finish_job
    owner.execute("update jobs set locked_until = now() - interval '1 second' where id=%s", (first["id"],))
    again = _claim(conn)  # gap 11: person2 hands the same running post job out a second time
    assert (again["id"], again["attempts"]) == (first["id"], 2) and again["claim_token"] != first["claim_token"]
    calls = len(fake.kinds)
    for state_dir in (s.state_dir, tmp_path / "lost_state"):  # with the agent's journal, and with it lost
        dup = TallyConnector(replace(s, state_dir=state_dir)).post_invoice(
            again["payload"], _auth(conn, cid, document, again["id"]))
        assert (dup.state, dup.erp_id) == (State.ALREADY_POSTED, out.erp_id)
        assert _report(svc, cid, document, again, dup) == {"status": "posted"}
    assert len(fake.kinds) == calls and len(fake.imports) == 1
    assert _finish(conn, first) is None and _finish(conn, again) == "done"  # the stale holder is fenced
    st = _p3(conn, cid, document, first["id"])
    assert (st.status, st.result, st.erp_id) == ("posted", "posted", out.erp_id) and len(st.events) == 1


def test_a_duplicate_delivery_through_the_agent_posts_once_and_reports_the_same_id(conn, world, document, fake,
                                                                                  tmp_path):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    sink = _P3Sink(svc, cid, document)
    results = AgentHost(_settings(fake, tmp_path, cid), _PlatformSource(conn, cid, document, job, deliveries=2),
                        sink).run(interval=0.01, max_ticks=3)
    assert [r.outcome.state if r.outcome else None for r in results] == [State.POSTED, State.ALREADY_POSTED, None]
    assert results[1].outcome.erp_id == results[0].outcome.erp_id and len(fake.imports) == 1
    assert sink.results == [{"status": "posted"}, {"status": "posted"}]
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("posted", "posted", results[0].outcome.erp_id)


def test_through_the_agent_a_line_that_needs_a_p2_mapping_is_refused_never_guessed(conn, world, document, p2_app,
                                                                                 tally, tmp_path):
    """``AgentHost`` calls ``post_invoice(payload, authorization)``: ``ClaimedJob`` has no slot for the client's mapping
    rows, so on the agent path a P2 mapping row is not applied and the draft is refused (missing_ledger), never posted
    under a guessed ledger. Pins that gap; when the agent can carry the mapping, this test changes with it."""
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    fake = tally(companies=[TEST_CO], ledgers=_book((VENDOR, CREDITORS), (TALLY_PURCHASE, "Purchase Accounts")),
                 vouchers={TEST_CO[1]: []})
    row = {"kind": "ledger", "our_key": PURCHASE, "erp_value": TALLY_PURCHASE}
    assert p2_app.http.put("/agent/mapping", headers=p2_app.agent, json=row).status_code == 200
    tick = _run_once(_settings(fake, tmp_path, cid), _PlatformSource(conn, cid, document, job),
                     _P3Sink(svc, cid, document))
    assert tick.outcome.state == State.VALIDATION_REJECTED and "missing_ledger" in dict(tick.outcome.problems)
    assert fake.imports == [] and tick.reported
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result) == ("exception", "failed") and st.error.startswith("validation_rejected: ")


# ====================== 17-18. uncertain outcome, restart ======================


@pytest.mark.parametrize("mode", ["hangup_before_create", "garbage", "created_not_stored", "created_wrong_amount"])
def test_an_uncertain_import_is_a_p3_exception_and_neither_a_restart_nor_a_redelivery_resends(conn, world, document,
                                                                                             fake, tmp_path, mode):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    before = _auth(conn, cid, document, job["id"])  # the rows as they were when the job was handed over
    s = _settings(fake, tmp_path, cid)
    fake.import_mode = mode
    sink = _P3Sink(svc, cid, document)
    tick = _run_once(s, _PlatformSource(conn, cid, document, job), sink)
    assert tick.outcome.state == State.OUTCOME_UNCERTAIN and tick.outcome.sent and tick.outcome.erp_id is None
    assert tick.reported and sink.results == [{"status": "exception"}] and len(fake.imports) == 1
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("exception", "failed", None)
    assert st.error.startswith(UNCERTAIN) and st.reason == st.error
    assert st.events == [("erp_failed", {"erp_id": None, "error": st.error})]

    # 18. a restarted agent on the same state gets the job again: with the rows as P3 holds them now, and with the
    # rows from before the report (a platform that never learnt the outcome); neither reaches the Import
    fake.import_mode = "create"
    again = [_run_once(s, src, _P3Sink(svc, cid, document)).outcome for src in (
        _PlatformSource(conn, cid, document, job), _PlatformSource(conn, cid, document, job, auth=before))]
    assert [o.state for o in again] == [State.NOT_AUTHORIZED, State.BLOCKED]
    assert {"not_pending", "not_approved"} <= set(dict(again[0].problems))
    assert "reconciliation_required" in dict(again[1].problems) and len(fake.imports) == 1
    assert _p3(conn, cid, document, job["id"]) == st  # P3 keeps the first report

    # reconciliation only reads: an absent or mismatching voucher never settles it (not_found is not proof)
    kinds = len(fake.kinds)
    assert reconcile(document, date.today(), s).state == State.OUTCOME_UNCERTAIN
    assert "import" not in fake.kinds[kinds:] and len(fake.imports) == 1


def test_a_send_that_tally_stored_before_hanging_up_is_reconciled_and_posted_in_p3(conn, world, document, fake,
                                                                                  tmp_path):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    fake.import_mode = "hangup_after_create"
    tick = _run_once(_settings(fake, tmp_path, cid), _PlatformSource(conn, cid, document, job),
                     _P3Sink(svc, cid, document))
    assert tick.outcome.state == State.RECONCILED and tick.reported and len(fake.imports) == 1
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("posted", "posted", tick.outcome.erp_id)


# ====================== 19. open bills and masters into person2 ======================


def test_open_bills_from_tally_land_in_p2_open_bills_through_the_agent_route(conn, world, p2_app, tally):
    cid = world["client"]["id"]
    fake = tally(companies=[TEST_CO], ledgers=_book((VENDOR, CREDITORS), ("Cash", "Cash-in-Hand")))
    fake.bills[TEST_CO[1]] = [bill("INV-42", VENDOR, "11800.00", "11800.00"),
                              bill("INV-43", VENDOR, "250.10", "0.10", billdate="20260402"),  # partly paid
                              bill("ADV-1", VENDOR, "-500.00", "-500.00"),  # an advance: a debit, not a payable
                              bill("C-1", "Cash", "50.00", "50.00")]  # not a creditor
    connector = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port,
                                             timeout=2))
    bills = connector.fetch_open_bills()
    assert {b.bill_ref for b, _ in bills.excluded} == {"ADV-1", "C-1"}  # kept with a reason, never sent
    r = p2_app.http.post("/agent/open-bills", headers=p2_app.agent, json=bills.platform_payload())
    assert (r.status_code, r.json()) == (201, {"open_bills": 2})
    with tenant(conn, cid):
        rows = conn.execute("select party_ledger, vendor_gstin, bill_ref, bill_date, due_date, amount, pending_amount, "
                            "as_of from open_bills order by bill_ref").fetchall()
    assert rows == [
        {"party_ledger": VENDOR, "vendor_gstin": None, "bill_ref": "INV-42", "bill_date": date(2026, 4, 1),
         "due_date": None, "amount": Decimal("11800.00"), "pending_amount": Decimal("11800.00"), "as_of": bills.as_of},
        {"party_ledger": VENDOR, "vendor_gstin": None, "bill_ref": "INV-43", "bill_date": date(2026, 4, 2),
         "due_date": None, "amount": Decimal("250.10"), "pending_amount": Decimal("0.10"), "as_of": bills.as_of}]
    assert "import" not in fake.kinds and fake.imports == []

    # an empty pull is a valid result: it replaces the client's open bills with none (never "keep the old ones")
    fake.bills[TEST_CO[1]] = []
    r = p2_app.http.post("/agent/open-bills", headers=p2_app.agent,
                           json=connector.fetch_open_bills().platform_payload())
    assert (r.status_code, r.json()) == (201, {"open_bills": 0})
    with tenant(conn, cid):
        assert conn.execute("select count(*) as n from open_bills").fetchone()["n"] == 0


@pytest.mark.parametrize("amount", ["0.01", "0.10", "1180.10", "99999999.99", "999999999999.99"])
def test_open_bill_amounts_reach_p2_exactly(conn, world, p2_app, tally, amount):
    """P2's OpenBill model reads amounts as float (gap 5); these values must still land exactly in numeric(14,2)."""
    fake = tally(companies=[TEST_CO], ledgers=_book((VENDOR, CREDITORS)))
    fake.bills[TEST_CO[1]] = [bill("INV-1", VENDOR, amount, amount)]
    payload = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=fake.port,
                                           timeout=2)).fetch_open_bills().platform_payload()
    assert payload["bills"][0]["amount"] == amount
    assert p2_app.http.post("/agent/open-bills", headers=p2_app.agent, json=payload).status_code == 201
    with tenant(conn, world["client"]["id"]):
        row = conn.execute("select amount, pending_amount from open_bills").fetchone()
    assert (row["amount"], row["pending_amount"]) == (Decimal(amount), Decimal(amount))
    assert str(row["amount"]) == amount


def test_the_real_tally_ledger_reply_lands_in_p2_masters_snapshots(conn, world, p2_app):
    companies = (REAL_MASTERS / "response_01_companies.xml").read_bytes()
    ledgers = (REAL_MASTERS / "response_02_ledgers.xml").read_bytes()
    srv = _FakeTally(b"", responder=lambda body, n: companies if b"<TYPE>Company</TYPE>" in body else ledgers)
    try:
        bodies = TallyConnector(AgentSettings(company_name=TEST_CO[0], company_guid=TEST_CO[1], port=srv.port,
                                              timeout=3)).masters_snapshot()
    finally:
        srv.close()
    assert not any(b"Import" in body for body in srv.received)
    for body in bodies:
        r = p2_app.http.post("/agent/masters", headers=p2_app.agent, json=body)
        assert (r.status_code, r.json()["items"]) == (201, len(body["items"]))
    with tenant(conn, world["client"]["id"]):
        rows = conn.execute("select kind, items, item_count, source from masters_snapshots order by id").fetchall()
    assert [(r["kind"], r["item_count"], r["source"]) for r in rows] == [
        ("company", 1, "tally_agent"), ("ledger", 4, "tally_agent")]
    assert rows[0]["items"] == [{"name": TEST_CO[0], "guid": TEST_CO[1]}]
    assert rows[1]["items"] == [{"name": "Cash", "parent": "Cash-in-Hand"},
                                {"name": "FinLedger Test Purchase", "parent": "Purchase Accounts"},
                                {"name": "FinLedger Test Vendor", "parent": "Sundry Creditors"},
                                {"name": "Profit & Loss A/c", "parent": "Primary"}]


# ====================== 20. errors -> person3's exception / review representation ======================


def _no_purchase_ledger(fake, document):
    fake.ledgers[TEST_CO[1]] = fake.ledgers[TEST_CO[1]][:1]


def _tally_rejects(fake, document):
    fake.import_mode = "reject"


def _connection_lost(fake, document):
    fake.import_mode = "hangup_before_create"


def _marker_already_in_tally(fake, document):
    fake.add_voucher(TEST_CO[1], narration=f"Imported by hand {identity_marker(document)}", reference="INV-42",
                     amount="11800.00")


@pytest.mark.parametrize("arrange,prefix,text,imports", [
    (_no_purchase_ledger, "validation_rejected: ", "missing_ledger", 0),
    (_tally_rejects, "tally_rejected: ", "Voucher totals do not match!", 1),
    (_connection_lost, UNCERTAIN, "lookup not_found", 1),
    (_marker_already_in_tally, "blocked: ", "already carries this document's marker", 0),
], ids=["validation_rejected", "tally_rejected", "outcome_uncertain", "reportable_block"])
def test_reportable_outcomes_reach_p3_as_an_exception_with_their_prefix(conn, world, document, fake, tmp_path, arrange,
                                                                        prefix, text, imports):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    arrange(fake, document)
    sink = _P3Sink(svc, cid, document)
    tick = _run_once(_settings(fake, tmp_path, cid), _PlatformSource(conn, cid, document, job), sink)
    assert tick.reported and sink.results == [{"status": "exception"}] and len(fake.imports) == imports
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("exception", "failed", None)
    assert st.error.startswith(prefix) and text in st.error and st.reason == st.error
    assert st.error == tick.outcome.erp_result()["error"]


def _tally_goes_down(fake, s, document, stack):
    fake.down = True


def _another_company_takes_the_name(fake, s, document, stack):
    """A same-named copy of the books that holds every ledger the draft names: only the paired-GUID check refuses it."""
    fake.companies = [(TEST_CO[0], OTHER_CO[1])]
    fake.ledgers[OTHER_CO[1]] = [ledger(VENDOR, CREDITORS, OTHER_CO[1], n=1),
                                 ledger(PURCHASE, "Purchase Accounts", OTHER_CO[1], n=2)]
    fake.vouchers[OTHER_CO[1]] = []


def _document_locked_elsewhere(fake, s, document, stack):
    stack.enter_context(FileJournal(s.journal_dir).lock(document))


def _cache_masters_then_down(fake, s, document, stack):
    save_masters_cache(s.masters_cache, TallyConnector(s).fetch_masters())
    fake.down = True


# case: (settings changes, connector type, after a passing startup, expected state, expected problem code)
NOT_REPORTED = {
    "posting_disabled": (dict(posting_enabled=False), None, None, None, None),
    "client_mismatch": (dict(client_id=OTHER_CLIENT), None, None, State.NOT_AUTHORIZED, "client_mismatch"),
    "tally_down": ({}, None, _tally_goes_down, State.TALLY_UNAVAILABLE, None),
    "company_guid_changed": ({}, None, _another_company_takes_the_name, State.BLOCKED, "company_unverified"),
    "send_not_delivered": ({}, _UndeliveredConnector, None, State.SEND_NOT_DELIVERED, None),
    "document_busy": ({}, None, _document_locked_elsewhere, State.BLOCKED, "document_busy"),
    "offline_xml_issued": (dict(offline_xml_enabled=True), None, _cache_masters_then_down, State.OFFLINE_XML, None),
}


@pytest.mark.parametrize("case", sorted(NOT_REPORTED))
def test_non_reportable_outcomes_leave_the_p3_document_approved(conn, world, document, fake, tmp_path, case):
    changes, connector_type, after_startup, state, code = NOT_REPORTED[case]
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    s = _settings(fake, tmp_path, cid, **changes)
    source, sink = _PlatformSource(conn, cid, document, job), _P3Sink(svc, cid, document)
    host = AgentHost(s, source, sink, connector=connector_type(s) if connector_type else None)
    host.startup()
    with ExitStack() as stack:
        if after_startup:
            after_startup(fake, s, document, stack)
        tick = host.tick()
    if state is None:  # not ready to post: nothing is even claimed
        assert tick.outcome is None and source.claims == 0
    else:
        assert tick.outcome.state == state and tick.outcome.erp_result() is None and source.handed == 1
        assert code is None or code in dict(tick.outcome.problems)
    if case == "company_guid_changed":  # refused by the GUID check itself, not by a later masters check
        detail = dict(tick.outcome.problems)["company_unverified"]
        assert TEST_CO[1] in detail and "paired GUID" in detail
        assert OTHER_CO[1] not in detail  # the other company's GUID is never quoted (round-2 security fix)
    assert not tick.reported and sink.results == [] and fake.imports == []
    assert _p3(conn, cid, document, job["id"]) == UNTOUCHED


# ====================== failure injection at the platform boundaries ======================


def test_p3_unreachable_while_reporting_a_post_then_a_later_delivery_reports_the_same_erp_id(conn, world, document,
                                                                                           fake, tmp_path, dead_conn):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    s = _settings(fake, tmp_path, cid)
    first = _run_once(s, _PlatformSource(conn, cid, document, job), _P3Sink(ControlService(dead_conn), cid, document))
    assert first.outcome.state == State.POSTED and not first.reported
    assert first.error_kind == "report failed: OperationalError"
    assert _p3(conn, cid, document, job["id"]) == UNTOUCHED  # P3 never heard; the journal holds the post

    sink = _P3Sink(svc, cid, document)  # a restarted agent; P3's rows still say pending
    again = _run_once(s, _PlatformSource(conn, cid, document, job), sink)
    assert (again.outcome.state, again.outcome.erp_id) == (State.ALREADY_POSTED, first.outcome.erp_id)
    assert again.reported and sink.results == [{"status": "posted"}] and len(fake.imports) == 1
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("posted", "posted", first.outcome.erp_id)


def test_p3_unreachable_while_reporting_an_uncertain_send_keeps_it_blocked_and_reports_it_later(conn, world, document,
                                                                                              fake, tmp_path,
                                                                                              dead_conn):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    s = _settings(fake, tmp_path, cid)
    fake.import_mode = "hangup_before_create"
    first = _run_once(s, _PlatformSource(conn, cid, document, job), _P3Sink(ControlService(dead_conn), cid, document))
    assert first.outcome.state == State.OUTCOME_UNCERTAIN and first.error_kind == "report failed: OperationalError"
    assert _p3(conn, cid, document, job["id"]) == UNTOUCHED

    fake.import_mode = "create"
    again = _run_once(s, _PlatformSource(conn, cid, document, job), _P3Sink(svc, cid, document))
    assert again.outcome.state == State.BLOCKED and "reconciliation_required" in dict(again.outcome.problems)
    assert again.reported and len(fake.imports) == 1
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("exception", "failed", None)
    # the late report comes from the journal block (today prefixed "blocked: "), and still names the uncertain send
    assert "outcome_uncertain" in st.error and st.reason == st.error


def test_p3_refusing_a_posted_result_after_an_edit_cannot_lead_to_a_second_voucher(conn, world, document, fake,
                                                                                  tmp_path):
    """Without a claim route (gap 10) nothing marks the post job running, so a reviewer can edit after P4 posted and
    before P3 heard. P3 then refuses the result (a real Conflict) and the ERP id exists only in P4's journal; the
    re-approved revision must still not reach Tally again."""
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    s = _settings(fake, tmp_path, cid)
    out = TallyConnector(s).post_invoice(job["payload"], _auth(conn, cid, document, job["id"]))
    assert out.state == State.POSTED
    rev = job["payload"]["revision"]
    svc.edit_field(cid, document, world["approver"], "narration", "edited after posting", expected_revision=rev)
    with pytest.raises(Conflict, match="in_review"):
        _report(svc, cid, document, job, out)
    st = _p3(conn, cid, document, job["id"])
    assert (st.status, st.result, st.erp_id) == ("in_review", "pending", None)

    svc.approve(cid, document, world["approver"], post=True, expected_revision=rev + 1)
    job2 = svc.post_job(cid, document)
    assert job2["id"] != job["id"] and job2["payload"]["revision"] == rev + 1
    tick = _run_once(s, _PlatformSource(conn, cid, document, job2), _P3Sink(svc, cid, document))
    assert tick.outcome.state == State.BLOCKED and "already_posted_other" in dict(tick.outcome.problems)
    assert tick.reported and len(fake.imports) == 1
    st2 = _p3(conn, cid, document, job2["id"])
    assert (st2.status, st2.result, st2.erp_id) == ("exception", "failed", None)
    assert st2.error.startswith("blocked: ") and out.erp_id in st2.error  # a person can reconcile from this text


def test_an_edit_is_refused_while_p2_holds_the_post_job_claimed(conn, world, document):
    cid = world["client"]["id"]
    svc, job = _approve(conn, world, document)
    assert _claim(conn)["id"] == job["id"]
    with pytest.raises(Conflict, match="active post"):
        svc.edit_field(cid, document, world["approver"], "narration", "edited", expected_revision=1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("failure,exc", [("closed_connection", "OperationalError"),
                                         ("unreachable_server", "OperationalError"),
                                         ("rows_of_another_tenant", "ValueError")])
def test_a_platform_database_that_cannot_be_read_posts_nothing(conn, world, document, fake, tmp_path, app_dsn,
                                                               dead_conn, failure, exc):
    cid = world["client"]["id"]
    _, job = _approve(conn, world, document)
    if failure == "closed_connection":
        source = _PlatformSource(dead_conn, cid, document, job)
    elif failure == "unreachable_server":
        info = conninfo_to_dict(app_dsn)
        info.update(host="127.0.0.1", port=str(_free_port()))
        source = _ConnectingSource(make_conninfo(**info), cid, document, job)
    else:  # read under another client's tenant: RLS returns nothing, and nothing is not an approval
        source = _PlatformSource(conn, OTHER_CLIENT, document, job)
    tick = _run_once(_settings(fake, tmp_path, cid), source, _P3Sink(ControlService(conn), cid, document))
    assert tick.claimed is False and tick.outcome is None and tick.error_kind == f"claim failed: {exc}"
    assert fake.imports == [] and "import" not in fake.kinds
    assert _p3(conn, cid, document, job["id"]) == UNTOUCHED


# ====================== contract checks against the running upstream code and schema (no Tally) ======================


def _allowed(conn, table: str, column: str) -> set[str]:
    """The values the one ``column = ANY (...)`` CHECK constraint of ``table`` allows, read from the migrated database
    (so a later ``alter table ... add constraint`` counts, which a regex over the migration files can miss)."""
    defs = [r["d"] for r in conn.execute("select pg_get_constraintdef(oid) as d from pg_constraint "
                                         "where conrelid = %s::regclass and contype = 'c'", (table,))
            if re.match(rf"CHECK \(\({column} = ANY", r["d"])]
    assert len(defs) == 1, defs
    return set(re.findall(r"'([^']+)'::text", defs[0]))


def _columns(conn, table: str) -> set[str]:
    return {r["column_name"] for r in conn.execute("select column_name from information_schema.columns "
                                                   "where table_schema = 'public' and table_name = %s", (table,))}


def test_p3_moves_a_document_to_posted_only_from_the_statuses_p4_posts_from(conn):
    """test_contract_compat's pin of this only reads P4's own constant; this asks person3's workflow about every
    document status person2's schema allows."""
    statuses = _allowed(conn, "documents", "status")
    ctx = TransitionContext("person4", set())

    def posts(status):
        try:
            return transition(status, Action.POSTED, ctx) == "posted"
        except InvalidTransition:
            return False
    assert {st for st in statuses if posts(st)} == set(POSTABLE_DOCUMENT_STATUSES)


def test_p4_erp_result_supplies_every_p3_erp_result_keyword_but_the_job_ids():
    params = inspect.signature(ControlService.erp_result).parameters
    keywords = {n for n, p in params.items() if p.kind is p.KEYWORD_ONLY}
    required = {n for n, p in params.items() if p.kind is p.KEYWORD_ONLY and p.default is p.empty}
    for outcome in (PostOutcome(State.POSTED, None, 1, "", erp_id="x"), PostOutcome(State.TALLY_REJECTED, None, 1, "")):
        keys = set(outcome.erp_result())
        assert keys <= keywords and required - keys == {"job_id", "revision", "idempotency_key"}


def test_the_running_p2_app_has_no_agent_route_to_claim_or_report_post_jobs(tmp_path):
    """Stronger than test_contract_compat's regex over api.py: routes registered any other way are seen too (gap 10)."""
    s = _p2_settings("postgresql://unused", "", tmp_path)
    app = create_app(s, None, LocalStore(s.store_root, s.signing_secret, s.app_base_url))
    assert sorted(r.path for r in app.routes if r.path.startswith("/agent")) == [
        "/agent/mapping", "/agent/masters", "/agent/open-bills"]


def test_the_migrated_schema_still_has_the_gaps_p4_works_around(conn):
    """The live-schema version of test_contract_compat's gap 1/2/3/14 pins. A failure means person2 changed the
    contract: update P4's mapping and docs/CONTRACT_GAPS.md together, then this test."""
    assert _allowed(conn, "posting_attempts", "result") == {"pending", "posted", "failed"}  # gap 1
    attempts, links = _columns(conn, "posting_attempts"), _columns(conn, "erp_links")
    assert {"result", "erp_id", "error"} <= attempts and "company_name" in links  # the tables are visible at all
    assert not {"request_sha256", "response_sha256", "response_excerpt"} & attempts  # gap 2
    assert "company_guid" not in links  # gap 3
    assert _allowed(conn, "approvals", "kind") == {"human", "policy_clear", "payment_release"}  # gap 14
    assert set(POSTING_APPROVAL_KINDS) < _allowed(conn, "approvals", "kind")
    assert {"superseded_at", "superseded_by_revision"} <= _columns(conn, "approvals")
    assert "cancelled" in _allowed(conn, "jobs", "state")
