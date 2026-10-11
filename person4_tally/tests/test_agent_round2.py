"""Round-2 hardening of the connector, the health report and the settings. SYNTHETIC Tally only (fake_tally.py on an
ephemeral 127.0.0.1 port); nothing here talks to a real Tally. The only Import any test lets through goes to a local
fake, in the control case that proves a valid job still posts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finledger_tally import (
    AgentHost,
    AgentSettings,
    CompanyNotResolved,
    Health,
    PostOutcome,
    State,
    TallyConnector,
    TallyUnavailable,
    p3_request_hash,
)
from finledger_tally.identity import TallyMalformedResponse
from test_agent_cli_hardening import cli, paired
from test_hardening import Sink, Source
from test_masters import BOOK
from test_posting import CLIENT, approval, make_job, phases, tally  # noqa: F401 (fixture)
from test_release_boundary import _env

# Two loaded companies whose names must never reach an operator's screen or log (client data).
CO_A = ("Secret Alpha Traders Pvt Ltd", "1a5f42d4-ec1d-4aab-9992-e2e217bcb701")
CO_B = ("Hidden Beta Exports LLP", "2a5f42d4-ec1d-4aab-9992-e2e217bcb702")
CO_A_TWIN = (CO_A[0].upper(), "3a5f42d4-ec1d-4aab-9992-e2e217bcb703")  # same name ignoring case
CO_A_OTHER_GUID = (CO_A[0], "4a5f42d4-ec1d-4aab-9992-e2e217bcb704")
SECRETS = (CO_A[0], CO_A[0].upper(), CO_B[0], CO_B[0].upper(), "Alpha", "ALPHA", "Beta", "BETA")


# ====================== 1. post methods never raise on a malformed job ======================


MALFORMED_JOBS = [None, [], [1], "job", 5, {}, {"voucher_draft": "x"}, {"voucher_draft": []},
                  {"voucher_draft": None}, {"voucher_draft": 7}, {"voucher_draft": {"voucher_type": ["purchase"]}},
                  {"voucher_draft": {"voucher_type": {"purchase": 1}}}]
METHODS = ["post_invoice", "post_credit_debit", "post_payment"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("posting", [True, False], ids=["posting", "not-posting"])
def test_every_post_method_returns_a_structured_refusal_for_a_malformed_job(tally, tmp_path, method, posting):
    fake = tally()
    connector = TallyConnector(paired(fake, tmp_path, posting_enabled=posting))
    for job in MALFORMED_JOBS:
        for auth in (None, [], "approved"):
            outcome = getattr(connector, method)(job, auth)
            assert isinstance(outcome, PostOutcome) and outcome.state == State.NOT_AUTHORIZED, (job, auth)
            assert not outcome.sent and outcome.erp_result() is None  # no approval: never reported
    assert fake.kinds == [] and fake.imports == []


@pytest.mark.parametrize("draft", ["x", [], None, 7, ["purchase"], {"voucher_type": ["purchase"]}],
                         ids=["str", "list", "none", "int", "list-of-type", "unhashable-type"])
@pytest.mark.parametrize("method", METHODS)
def test_an_approved_job_with_a_non_object_draft_is_validation_rejected_not_an_exception(tally, tmp_path, draft,
                                                                                        method):
    fake = tally()
    job = make_job()
    job["voucher_draft"] = draft
    outcome = getattr(TallyConnector(paired(fake, tmp_path)), method)(
        job, approval(job, attempt_request_hash=p3_request_hash(job)))
    assert isinstance(outcome, PostOutcome) and outcome.state == State.VALIDATION_REJECTED and not outcome.sent
    assert [c for c, _ in outcome.problems] == ["unsupported_voucher_type"]
    assert outcome.erp_result()["error"].startswith("validation_rejected: ")
    assert len(outcome.detail) < 400  # the draft value is quoted bounded, never whole
    assert fake.kinds == [] and fake.imports == []


@pytest.mark.parametrize("mapping", ["x", ["ledger"], {"ledger": {}}, 0, b"rows"],
                         ids=["str", "list", "plain-dict", "zero", "bytes"])
def test_a_mapping_that_is_not_a_mapping_table_is_refused_before_any_request(tally, tmp_path, mapping):
    fake = tally()
    settings = paired(fake, tmp_path)
    job = make_job()
    outcome = TallyConnector(settings).post_invoice(job, approval(job), mapping=mapping)
    assert isinstance(outcome, PostOutcome) and outcome.state == State.BLOCKED and not outcome.sent
    assert [c for c, _ in outcome.problems] == ["invalid_argument"]
    assert outcome.erp_result() is None  # a caller bug never moves the document to the exception tray
    assert fake.kinds == [] and fake.imports == [] and phases(settings) == []


def test_a_valid_job_without_a_mapping_still_posts(tally, tmp_path):
    fake = tally()
    job = make_job()
    outcome = TallyConnector(paired(fake, tmp_path)).post_invoice(job, approval(job), mapping=None)
    assert outcome.state == State.POSTED and len(fake.imports) == 1


# ====================== 2. the ambiguous-company masters label ======================


@pytest.mark.parametrize("companies", [[CO_A, CO_A_TWIN, CO_B], [CO_A, CO_A_OTHER_GUID, CO_B]],
                         ids=["case-twin", "exact-twin"])
def test_an_ambiguous_company_says_its_masters_were_not_read(tally, tmp_path, companies):
    fake = tally(companies=companies)
    r = TallyConnector(paired(fake, tmp_path, company_name=CO_A[0], company_guid=CO_A[1])).health()
    assert r.status == Health.COMPANY_AMBIGUOUS
    assert r.masters == "failed: CompanyNotResolved (company ambiguous; masters not read)"
    assert fake.kinds == ["Company"]  # the label is honest: no masters request was made


# ====================== 3. no other company's name in health or CLI output ======================


def _leaky_unavailable(*_a, **_k):
    raise TallyUnavailable(f"Tally did not answer on 127.0.0.1:9: OSError: loaded {CO_A[0]}, {CO_B[0]}")


def _leaky_unavailable_free_text(*_a, **_k):
    raise TallyUnavailable(f"companies {CO_A[0]} and {CO_B[0]} went away")


def _leaky_not_resolved(*_a, **_k):
    raise CompanyNotResolved(f"2 loaded companies match: {CO_A[0]!r}, {CO_B[0]!r}")


def _leaky_malformed(*_a, **_k):
    raise TallyMalformedResponse(f"ledger list names {CO_B[0]} beside {CO_A[0]}")


def _with_companies(fake, changes):
    """The CLI environment for company A on ``fake``, with ``changes`` applied (None removes a variable)."""
    env = {**_env(fake, FINLEDGER_TALLY_COMPANY=CO_A[0], FINLEDGER_TALLY_COMPANY_GUID=CO_A[1]), **changes}
    return {k: v for k, v in env.items() if v is not None}


NOT_READY = {
    # name: (companies, patch target, patch, env changes, expected reason)
    "unreachable": ([CO_A, CO_B], None, None, {}, Health.TALLY_UNREACHABLE),
    "unreachable_message_names": ([CO_A, CO_B], "list_companies", _leaky_unavailable, {}, Health.TALLY_UNREACHABLE),
    "unreachable_free_text": ([CO_A, CO_B], "list_companies", _leaky_unavailable_free_text, {},
                              Health.TALLY_UNREACHABLE),
    "malformed_reply": ([CO_A, CO_B], "reply", None, {}, Health.MALFORMED_REPLY),
    "list_raises_names": ([CO_A, CO_B], "list_companies", _leaky_malformed, {}, Health.MALFORMED_REPLY),
    "not_configured": ([CO_A, CO_B], None, None, {"FINLEDGER_TALLY_COMPANY": None,
                                                  "FINLEDGER_TALLY_COMPANY_GUID": None}, Health.COMPANY_NOT_CONFIGURED),
    "unresolved": ([CO_A, CO_B], None, None, {"FINLEDGER_TALLY_COMPANY": "Neither Of Them"},
                   Health.COMPANY_UNRESOLVED),
    "ambiguous_case": ([CO_A, CO_A_TWIN, CO_B], None, None, {}, Health.COMPANY_AMBIGUOUS),
    "ambiguous_exact": ([CO_A, CO_A_OTHER_GUID, CO_B], None, None, {}, Health.COMPANY_AMBIGUOUS),
    "guid_mismatch": ([CO_A_OTHER_GUID, CO_B], None, None, {}, Health.COMPANY_GUID_MISMATCH),
    "masters_not_resolved_names": ([CO_A, CO_B], "fetch_ledger_masters", _leaky_not_resolved, {}, "masters_failed"),
    "masters_malformed_names": ([CO_A, CO_B], "fetch_ledger_masters", _leaky_malformed, {}, "masters_failed"),
}


def _not_ready(name, tally, monkeypatch):
    companies, target, patch, changes, _ = NOT_READY[name]
    fake = tally(companies=companies, ledgers={c[1]: BOOK for c in companies})
    if name == "unreachable":
        fake.down = True
    elif target == "reply":  # Tally's own error line naming both loaded companies
        fake.company_reply = (f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>"
                              f"<LINEERROR>Companies '{CO_A[0]}' and '{CO_B[0]}' are open</LINEERROR></DATA></BODY>"
                              f"</ENVELOPE>").encode()
    elif target is not None:
        monkeypatch.setattr(f"finledger_tally.connector.{target}", patch)
    return fake, _with_companies(fake, changes)


@pytest.mark.parametrize("name", NOT_READY, ids=list(NOT_READY))
def test_cli_health_names_no_loaded_company_on_any_not_ready_path(tally, monkeypatch, name):
    fake, env = _not_ready(name, tally, monkeypatch)
    text = cli(["config"], env)[1] + cli(["health"], env)[1]
    code, health = cli(["health"], env)
    body = json.loads(health)
    assert (code, body["ok"], body["reason"]) == (2, False, NOT_READY[name][4])
    for secret in SECRETS:
        assert secret not in text, (name, secret)
    assert fake.imports == []


@pytest.mark.parametrize("name", NOT_READY, ids=list(NOT_READY))
def test_agent_status_names_no_loaded_company_on_any_not_ready_path(tally, monkeypatch, name):
    fake, env = _not_ready(name, tally, monkeypatch)
    host = AgentHost(AgentSettings.from_env(env), Source([]), Sink())
    report = host.startup()
    shown = json.dumps(host.status()) + json.dumps(report.__dict__ if report else None)
    assert host.ready_to_post is False
    for secret in SECRETS:
        assert secret not in shown, (name, secret)


def test_an_unreachable_detail_keeps_only_the_error_type(tally, tmp_path, monkeypatch):
    fake = tally(companies=[CO_A, CO_B])
    connector = TallyConnector(paired(fake, tmp_path, company_name=CO_A[0], company_guid=CO_A[1]))
    monkeypatch.setattr("finledger_tally.connector.list_companies", _leaky_unavailable)
    assert connector.health().detail == f"Tally did not answer on 127.0.0.1:{fake.port}: OSError"
    monkeypatch.setattr("finledger_tally.connector.list_companies", _leaky_unavailable_free_text)
    assert connector.health().detail == f"Tally did not answer on 127.0.0.1:{fake.port}: TallyUnavailable"


def test_a_real_refused_connection_still_names_its_cause(tally, tmp_path):
    fake = tally()
    port = fake.port
    fake.close()  # nothing listens on the port any more
    r = TallyConnector(paired(fake, tmp_path)).health()
    assert r.status == Health.TALLY_UNREACHABLE and not r.tally_reachable
    assert r.detail.startswith(f"Tally did not accept a connection on 127.0.0.1:{port}: ")
    assert r.detail.rsplit(": ", 1)[1].isidentifier()  # a type name such as ConnectionRefusedError


# ====================== 4. the state folder is shown as absolute or not, never resolved ======================


@pytest.mark.parametrize("raw", ["relative/state", "state", "./state", "~/state"])
def test_from_env_keeps_a_relative_state_folder_relative(raw):
    s = AgentSettings.from_env({"FINLEDGER_TALLY_STATE_DIR": raw})
    assert s.state_dir == Path(raw) and not Path(s.state_dir).is_absolute()  # not pinned to today's cwd
    assert s.redacted()["state_dir_absolute"] is False
    posting = AgentSettings.from_env({"FINLEDGER_TALLY_STATE_DIR": raw, "FINLEDGER_TALLY_POSTING": "enabled",
                                      "FINLEDGER_TALLY_CLIENT_ID": CLIENT, "FINLEDGER_TALLY_COMPANY": CO_A[0],
                                      "FINLEDGER_TALLY_COMPANY_GUID": CO_A[1]})
    assert [c for c, _ in posting.posting_problems()] == ["state_dir_not_absolute"]


def test_cli_config_shows_whether_the_state_folder_is_absolute_but_not_its_path(tmp_path):
    private = tmp_path / "harshith-private" / "state"
    for raw, absolute in ((str(private), True), ("harshith-private/state", False)):
        code, text = cli(["config"], {"FINLEDGER_TALLY_STATE_DIR": raw})
        assert code == 0 and json.loads(text)["settings"]["state_dir_absolute"] is absolute
        assert "harshith-private" not in text
