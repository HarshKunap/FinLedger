"""Agent host, operator CLI, configuration and connector-method hardening. SYNTHETIC Tally only (fake_tally.py on an
ephemeral 127.0.0.1 port); nothing here talks to a real Tally, and no test sends Import XML anywhere but a local fake.

Restart tests here are the ones test_agent.py / test_release_boundary.py do not already hold: restart after a
persistence failure (before sending, after a failed report, a torn journal, an unusable state folder), restart while
Tally is unavailable, restart with a malformed job, shutdown during an in-flight Import, and status after a restart.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from finledger_tally import (
    AgentAlreadyRunning,
    AgentHost,
    AgentSettings,
    ClaimedJob,
    CompanyNotResolved,
    Health,
    InstanceLock,
    Phase,
    PostOutcome,
    State,
    TallyConnector,
    TallyTransport,
    TallyUnavailable,
)
from fake_tally import FakeTally
from finledger_tally.__main__ import main
from finledger_tally.agent import stop_on_signals
from test_contract_compat import P2_API, P3_SERVICE
from test_hardening import Sink, Source, claimed, fail_on
from test_isolation_check import OTHER_CO, TEST_CO
from test_masters import BOOK, ledger
from test_posting import CLIENT, NEW_DOC, approval, make_job, phases, settings_for, tally  # noqa: F401 (fixture)
from test_release_boundary import _env

DOC_B = "7c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"
DOC_C = "8c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"
DOC_D = "9c1c2d3e-5f60-4718-8a9b-0c1d2e3f4a5b"
TWIN_GUID = "0b1c2d3e-4f50-4a61-8b72-c83d94e5f699"  # a third company (OTHER_CO stays loaded beside it)
CASE_TWIN = (TEST_CO[0].upper(), TWIN_GUID)  # same name ignoring case, another company
EXACT_TWIN = (TEST_CO[0], TWIN_GUID)  # the very same name, another company
SECRET_LEDGER = "Shree Ganesh Metals"
SUCCESS = (State.POSTED, State.ALREADY_POSTED, State.RECONCILED)
NO_TALLY = SimpleNamespace(port=9000)  # for settings that are only inspected, never used to connect


class Repeat:
    """A platform that keeps re-delivering the same claim (or keeps failing the same way)."""

    def __init__(self, item: Any = None, error: BaseException | None = None):
        self.item, self.error, self.claims = item, error, 0

    def claim(self):
        self.claims += 1
        if self.error is not None:
            raise self.error
        return self.item


class InFlightTally(FakeTally):
    """Creates the voucher like ``create`` mode, but only ``slow_seconds`` after the Import arrived; ``import_started``
    is set the moment it arrives (an Import that is in flight). The base ``slow`` mode never stores the voucher."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.import_started = threading.Event()

    def _import(self, body, root):
        self.import_started.set()
        time.sleep(self.slow_seconds)
        return super()._import(body, root)


def cli(args, env):
    out = io.StringIO()
    code = main(args, env, out)
    return code, out.getvalue()


def paired(fake, tmp_path, **changes):
    """Connector-level settings of a paired, posting agent (absolute state folder)."""
    return settings_for(fake, tmp_path, **changes)


def never_imports(monkeypatch) -> list:
    """Make TallyTransport.import_vouchers record and refuse every call."""
    calls: list = []

    def refuse(self, body):
        calls.append(body)
        raise AssertionError("an Import was attempted")
    monkeypatch.setattr(TallyTransport, "import_vouchers", refuse)
    return calls


# ====================== configuration ======================


def test_posting_problems_are_empty_while_posting_is_off(tmp_path):
    assert AgentSettings().posting_problems() == ()
    assert AgentSettings(state_dir=Path("relative"), client_id=CLIENT).posting_problems() == ()


@pytest.mark.parametrize("changes,codes", [
    ({"client_id": None}, {"not_paired"}),
    ({"company_name": None}, {"not_paired"}),
    ({"company_guid": None}, {"not_paired"}),
    ({"state_dir": Path("finledger_tally_state")}, {"state_dir_not_absolute"}),
    ({"state_dir": "state"}, {"state_dir_not_absolute"}),
    ({"company_guid": None, "state_dir": Path("x")}, {"not_paired", "state_dir_not_absolute"}),
])
def test_posting_problems_name_every_missing_piece(tmp_path, changes, codes):
    s = paired(NO_TALLY, tmp_path, **changes)
    assert {c for c, _ in s.posting_problems()} == codes


def test_a_complete_posting_configuration_has_no_problems(tmp_path):
    assert paired(NO_TALLY, tmp_path).posting_problems() == ()


@pytest.mark.parametrize("key,value", [
    ("FINLEDGER_TALLY_PORT", "9_000"), ("FINLEDGER_TALLY_PORT", "٩٠٠٠"),  # Arabic-Indic 9000
    ("FINLEDGER_TALLY_PORT", "+9000"), ("FINLEDGER_TALLY_PORT", " 9000"), ("FINLEDGER_TALLY_PORT", "9000.0"),
    ("FINLEDGER_TALLY_TIMEOUT", "nan"), ("FINLEDGER_TALLY_TIMEOUT", "inf"), ("FINLEDGER_TALLY_TIMEOUT", "1e1"),
    ("FINLEDGER_TALLY_TIMEOUT", "-1"), ("FINLEDGER_TALLY_TIMEOUT", "30."), ("FINLEDGER_TALLY_MAX_REPLY_BYTES", "64MB"),
])
def test_numbers_in_the_environment_must_be_plain_ascii(key, value):
    with pytest.raises(ValueError, match=key):
        AgentSettings.from_env({key: value})


def test_plain_numbers_in_the_environment_are_read():
    s = AgentSettings.from_env({"FINLEDGER_TALLY_PORT": "9001", "FINLEDGER_TALLY_TIMEOUT": "2.5",
                                "FINLEDGER_TALLY_MAX_REPLY_BYTES": "4096"})
    assert (s.port, s.timeout, s.max_reply_bytes) == (9001, 2.5, 4096)


@pytest.mark.parametrize("state_dir", [None, 42, "state\x00dir"])
def test_state_dir_must_be_a_path(state_dir):
    with pytest.raises(ValueError):
        AgentSettings(state_dir=state_dir)


def test_redacted_settings_hold_no_company_name_and_no_state_path(tmp_path):
    s = AgentSettings(client_id=CLIENT, company_name="Secret Traders Pvt Ltd", company_guid=TEST_CO[1],
                      state_dir=tmp_path / "harshith-private" / "state")
    shown = json.dumps(s.redacted())
    assert "Secret Traders" not in shown and "harshith-private" not in shown and TEST_CO[1] not in shown
    assert s.redacted()["state_dir_absolute"] is True and s.redacted()["client_id"] == CLIENT
    assert AgentSettings(state_dir=Path("rel")).redacted()["state_dir_absolute"] is False


# ====================== operator CLI ======================


def _scenario(name, tally, tmp_path):
    """(fake, env) for one Tally state; every env is a valid configuration."""
    if name == "verified":
        fake = tally()
    elif name == "tally_down":
        fake = tally()
        fake.down = True
    elif name == "malformed":
        fake = tally()
        fake.company_reply = b"<ENVELOPE><BODY>"
    elif name == "tally_error_line":  # Tally's own error text may quote a company name
        fake = tally()
        fake.company_reply = (b"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>"
                              b"<LINEERROR>Company 'FinLedger Test Company' is not open</LINEERROR></DATA></BODY>"
                              b"</ENVELOPE>")
    elif name == "no_company":
        fake = tally(companies=[])
    elif name == "not_loaded":
        fake = tally(companies=[OTHER_CO])
    elif name == "case_twin":
        fake = tally(companies=[TEST_CO, CASE_TWIN])
    elif name == "exact_twin":
        fake = tally(companies=[TEST_CO, EXACT_TWIN])
    elif name == "guid_mismatch":
        fake = tally(companies=[EXACT_TWIN])
    elif name == "masters_name_in_error":  # the parser's message names the ledger
        fake = tally(ledgers={TEST_CO[1]: BOOK + [ledger(SECRET_LEDGER, "", n=0x99)]})
    elif name == "masters_foreign_ledger":  # CompanyNotResolved naming another company's ledger
        fake = tally(ledgers={TEST_CO[1]: BOOK + [ledger(SECRET_LEDGER, "Sundry Creditors", OTHER_CO[1], n=0x98)]})
    elif name == "posting_agent_verified":  # the most dangerous valid configuration: posting on and paired
        fake = tally()
        return fake, _env(fake, FINLEDGER_TALLY_POSTING="enabled", FINLEDGER_TALLY_CLIENT_ID=CLIENT,
                          FINLEDGER_TALLY_STATE_DIR=str(tmp_path / "state"))
    else:
        raise AssertionError(name)
    return fake, _env(fake)


HEALTH_CASES = {
    "verified": (0, "ok"),
    "posting_agent_verified": (0, "ok"),
    "tally_down": (2, Health.TALLY_UNREACHABLE),
    "malformed": (2, Health.MALFORMED_REPLY),
    "tally_error_line": (2, Health.MALFORMED_REPLY),
    "no_company": (2, Health.NO_COMPANY_LOADED),
    "not_loaded": (2, Health.COMPANY_UNRESOLVED),
    "case_twin": (2, Health.COMPANY_AMBIGUOUS),
    "exact_twin": (2, Health.COMPANY_AMBIGUOUS),
    "guid_mismatch": (2, Health.COMPANY_GUID_MISMATCH),
    "masters_name_in_error": (2, "masters_failed"),
    "masters_foreign_ledger": (2, "masters_failed"),
}


@pytest.mark.parametrize("name", HEALTH_CASES, ids=list(HEALTH_CASES))
def test_cli_health_gives_a_distinct_reason_and_a_useful_exit_code(tally, tmp_path, name):
    fake, env = _scenario(name, tally, tmp_path)
    code, text = cli(["health"], env)
    body = json.loads(text)
    assert (code, body["reason"]) == HEALTH_CASES[name]
    assert body["ok"] is (code == 0)
    assert body["health"]["company_verified"] is (body["reason"] in ("ok", "masters_failed"))  # identity vs ledgers


@pytest.mark.parametrize("name", HEALTH_CASES, ids=list(HEALTH_CASES))
def test_cli_health_and_config_never_send_an_import(tally, tmp_path, monkeypatch, name):
    calls = never_imports(monkeypatch)
    fake, env = _scenario(name, tally, tmp_path)
    for command in ("config", "health"):
        code, _ = cli([command], env)
        assert code in (0, 2)
    assert calls == [] and fake.imports == [] and "import" not in fake.kinds


@pytest.mark.parametrize("name", HEALTH_CASES, ids=list(HEALTH_CASES))
def test_cli_output_never_names_the_company_or_a_ledger(tally, tmp_path, name):
    fake, env = _scenario(name, tally, tmp_path)
    text = cli(["config"], env)[1] + cli(["health"], env)[1]
    for secret in (TEST_CO[0], TEST_CO[0].upper(), OTHER_CO[0], SECRET_LEDGER, "FinLedger Test Vendor", "Other Co"):
        assert secret not in text


def test_cli_health_works_while_an_agent_holds_the_lock_and_writes_nothing(tally, tmp_path):
    fake = tally()
    state = tmp_path / "state"
    with InstanceLock(state):  # a running agent holds the state folder
        code, text = cli(["health"], _env(fake, FINLEDGER_TALLY_STATE_DIR=str(state)))
    assert code == 0 and json.loads(text)["ok"] is True
    assert [p.name for p in state.iterdir()] == ["agent.lock"]  # nothing but the lock this test took


def test_cli_health_into_a_fresh_state_folder_creates_nothing(tally, tmp_path):
    fake = tally()
    state = tmp_path / "never-created"
    code, _ = cli(["health"], _env(fake, FINLEDGER_TALLY_STATE_DIR=str(state)))
    assert code == 0 and not state.exists()


@pytest.mark.parametrize("extra,codes", [
    ({"FINLEDGER_TALLY_POSTING": "enabled", "FINLEDGER_TALLY_CLIENT_ID": CLIENT}, ["state_dir_not_absolute"]),
    ({"FINLEDGER_TALLY_POSTING": "enabled", "FINLEDGER_TALLY_CLIENT_ID": CLIENT,
      "FINLEDGER_TALLY_STATE_DIR": "relative/state"}, ["state_dir_not_absolute"]),
    ({"FINLEDGER_TALLY_POSTING": "enabled", "FINLEDGER_TALLY_STATE_DIR": "ABSOLUTE"}, ["not_paired"]),
    ({"FINLEDGER_TALLY_POSTING": "enabled", "FINLEDGER_TALLY_CLIENT_ID": CLIENT, "FINLEDGER_TALLY_COMPANY_GUID": "",
      "FINLEDGER_TALLY_STATE_DIR": "ABSOLUTE"}, ["not_paired"]),
])
def test_cli_refuses_a_posting_configuration_the_agent_would_refuse(tally, tmp_path, extra, codes):
    fake = tally()
    extra = {k: (str(tmp_path / "state") if v == "ABSOLUTE" else v) for k, v in extra.items()}
    for command in ("config", "health"):
        code, text = cli([command], _env(fake, **extra))
        body = json.loads(text)
        assert (code, body["ok"], body["reason"], body["problems"]) == (1, False, "invalid_configuration", codes)
        assert TEST_CO[0] not in text
    assert fake.kinds == []  # an invalid configuration never reaches Tally


def test_cli_accepts_a_complete_posting_configuration(tally, tmp_path):
    fake = tally()
    env = _env(fake, FINLEDGER_TALLY_POSTING="enabled", FINLEDGER_TALLY_CLIENT_ID=CLIENT,
               FINLEDGER_TALLY_STATE_DIR=str(tmp_path / "state"))
    code, text = cli(["config"], env)
    body = json.loads(text)
    assert code == 0 and body["settings"]["posting_enabled"] is True and body["warnings"] == []


@pytest.mark.parametrize("key", ["FINLEDGER_TALLY_POSTING", "FINLEDGER_TALLY_OFFLINE_XML"])
@pytest.mark.parametrize("value", ["Enabled", "true", "1", "enabled "])
def test_cli_config_says_a_mistyped_switch_is_off(key, value):
    code, text = cli(["config"], {key: value})
    body = json.loads(text)
    assert code == 0 and body["settings"]["posting_enabled"] is False
    assert body["settings"]["offline_xml_enabled"] is False
    assert any(key in w and "OFF" in w for w in body["warnings"])


def test_cli_config_shows_exactly_the_redacted_settings(tally, tmp_path):
    env = {"FINLEDGER_TALLY_COMPANY": "Secret Traders Pvt Ltd", "FINLEDGER_TALLY_CLIENT_ID": CLIENT,
           "FINLEDGER_TALLY_COMPANY_GUID": TEST_CO[1]}
    code, text = cli(["config"], env)
    assert code == 0 and json.loads(text)["settings"] == AgentSettings.from_env(env).redacted()
    assert CLIENT in text and "Secret Traders" not in text  # the client id is an identifier; the name is client data


@pytest.mark.parametrize("env,reason", [({"FINLEDGER_TALLY_PORT": "0"}, "invalid_configuration"),
                                        ({"FINLEDGER_TALLY_COMPANY": " padded"}, "invalid_configuration")])
def test_cli_invalid_configuration_has_its_own_reason(env, reason):
    for command in ("config", "health"):
        code, text = cli([command], env)
        assert (code, json.loads(text)["reason"]) == (1, reason)
        assert "padded" not in text.replace("empty, padded, or", "")  # the configured value is never echoed


@pytest.mark.parametrize("name", [" Secret Traders", "Secret Traders ", "Secret\tTraders", "Secret Traders\n",
                                  "Secret\x00Traders", "\u00a0Secret Traders"],
                         ids=["lead-space", "trail-space", "tab", "newline", "nul", "nbsp"])
@pytest.mark.parametrize("command", ["config", "health"])
def test_cli_never_echoes_an_invalid_company_name(tally, tmp_path, name, command):
    fake = tally()
    code, text = cli([command], {**_env(fake), "FINLEDGER_TALLY_COMPANY": name})
    body = json.loads(text)
    assert (code, body["reason"], body["ok"]) == (1, "invalid_configuration", False)
    assert "FINLEDGER_TALLY_COMPANY" in body["error"]  # the field is named so the operator can fix it
    for part in ("Secret", "Traders", repr(name)[1:-1]):
        assert part not in text
    assert fake.kinds == [] and fake.imports == []  # refused before any request


def test_the_settings_error_names_the_field_not_the_value():
    with pytest.raises(ValueError) as err:
        AgentSettings(company_name=" Secret Traders")
    assert "FINLEDGER_TALLY_COMPANY" in str(err.value) and "Secret" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_the_cli_withholds_any_settings_message_that_holds_the_company_name(monkeypatch):
    def leaky(env=None):
        raise ValueError(f"bad value {env['FINLEDGER_TALLY_COMPANY']!r} somewhere")
    monkeypatch.setattr(AgentSettings, "from_env", staticmethod(leaky))
    code, text = cli(["config"], {"FINLEDGER_TALLY_COMPANY": "Secret Traders"})
    assert (code, json.loads(text)["reason"]) == (1, "invalid_configuration") and "Secret" not in text


def test_cli_internal_error_has_its_own_reason(tally, monkeypatch):
    fake = tally()

    def boom(self, **kw):
        raise ZeroDivisionError(TEST_CO[0])
    monkeypatch.setattr(TallyConnector, "health", boom)
    code, text = cli(["health"], _env(fake))
    assert (code, json.loads(text)["reason"]) == (3, "internal_error") and TEST_CO[0] not in text


# ====================== connector: health ======================


@pytest.mark.parametrize("companies", [[TEST_CO, CASE_TWIN], [CASE_TWIN, TEST_CO], [TEST_CO, EXACT_TWIN]])
@pytest.mark.parametrize("check_masters", [True, False])
def test_health_never_verifies_an_ambiguous_company(tally, tmp_path, companies, check_masters):
    fake = tally(companies=companies)
    r = TallyConnector(paired(fake, tmp_path)).health(check_masters=check_masters)
    assert (r.status, r.company_verified, r.read_only_available, r.company_guid, r.posting) == \
        (Health.COMPANY_AMBIGUOUS, False, False, None, "enabled_but_not_ready")
    assert r.masters.startswith("failed: CompanyNotResolved") if check_masters else r.masters == "not_checked"
    assert fake.kinds == ["Company"]  # nothing was pinned to either company
    assert TEST_CO[0] not in r.detail and TEST_CO[0].upper() not in r.detail


@pytest.mark.parametrize("name", ["tally_error_line", "masters_name_in_error", "masters_foreign_ledger", "malformed",
                                  "tally_down", "case_twin"])
def test_health_text_fields_quote_no_reply_text(tally, tmp_path, name):
    fake, _ = _scenario(name, tally, tmp_path)
    r = TallyConnector(paired(fake, tmp_path)).health()
    text = f"{r.detail} {r.masters}"
    assert len(r.detail) <= 300 and len(r.masters) <= 300
    for secret in (TEST_CO[0], TEST_CO[0].upper(), SECRET_LEDGER, "is not open"):
        assert secret not in text


def test_a_connector_needs_agent_settings():
    with pytest.raises(TypeError):
        TallyConnector({"company_name": TEST_CO[0]})


# ====================== connector: reads ======================


READS = ["connect", "fetch_masters", "masters_snapshot", "fetch_open_bills"]


@pytest.mark.parametrize("method", READS)
def test_reads_without_a_company_name_send_nothing(tally, method):
    fake = tally()
    with pytest.raises(CompanyNotResolved):
        getattr(TallyConnector(AgentSettings(port=fake.port, timeout=2)), method)()
    assert fake.kinds == []


@pytest.mark.parametrize("method", READS)
@pytest.mark.parametrize("companies", [[EXACT_TWIN], [TEST_CO, CASE_TWIN]])
def test_reads_of_a_paired_agent_never_pin_another_or_an_ambiguous_company(tally, tmp_path, method, companies):
    fake = tally(companies=companies)
    with pytest.raises(CompanyNotResolved):
        getattr(TallyConnector(paired(fake, tmp_path)), method)()
    assert set(fake.kinds) == {"Company"} and fake.imports == []


def test_reads_never_import(tally, tmp_path, monkeypatch):
    calls = never_imports(monkeypatch)
    fake = tally()
    c = TallyConnector(paired(fake, tmp_path))
    for method in ["health"] + READS:
        getattr(c, method)()
    assert calls == [] and fake.imports == [] and "import" not in fake.kinds


def _p2_models() -> dict[str, Any]:
    """Person 2's own MastersIn / OpenBill / OpenBillsIn class bodies, taken from api.py (fastapi and psycopg are not
    needed to define them), built with pydantic."""
    pydantic = pytest.importorskip("pydantic")
    tree = ast.parse(P2_API.read_text())
    names = ("MastersIn", "OpenBill", "OpenBillsIn")
    wanted = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    assert [n.name for n in wanted] == ["MastersIn", "OpenBill", "OpenBillsIn"]
    ns: dict[str, Any] = {"BaseModel": pydantic.BaseModel, "Field": pydantic.Field, "Literal": Literal, "Any": Any,
                          "datetime": datetime}
    # dont_inherit: this test module's ``from __future__ import annotations`` must not leak into P2's class bodies
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(P2_API), "exec", dont_inherit=True), ns)  # noqa: S102
    for name in ("MastersIn", "OpenBill", "OpenBillsIn"):
        ns[name].model_rebuild(_types_namespace=ns)
    return ns


def test_masters_snapshot_and_open_bills_validate_against_person2_models(tally, tmp_path):
    from fake_tally import bill
    models = _p2_models()
    fake = tally()
    fake.bills[TEST_CO[1]] = [bill("TEST-INV-001", "FinLedger Test Vendor", "1180.00", "1180.00")]
    c = TallyConnector(paired(fake, tmp_path))
    bodies = c.masters_snapshot()
    assert [models["MastersIn"].model_validate(b).kind for b in bodies] == ["company", "ledger"]
    parsed = models["OpenBillsIn"].model_validate(c.fetch_open_bills().platform_payload())
    assert [(b.bill_ref, b.due_date, b.vendor_gstin) for b in parsed.bills] == [("TEST-INV-001", None, None)]


# ====================== connector: post methods ======================


def _p3_erp_result_keywords() -> tuple[set[str], set[str]]:
    """(all keyword-only parameters, those without a default) of person3 ControlService.erp_result, read from source."""
    tree = ast.parse(P3_SERVICE.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "erp_result")
    names = [a.arg for a in fn.args.kwonlyargs]
    required = {a for a, d in zip(names, fn.args.kw_defaults) if d is None}
    return set(names), required


@pytest.mark.parametrize("outcome", [
    PostOutcome(State.POSTED, NEW_DOC, 1, "x", erp_id=f"{TEST_CO[1]}-00000001"),
    PostOutcome(State.ALREADY_POSTED, NEW_DOC, 1, "x", erp_id=f"{TEST_CO[1]}-00000001"),
    PostOutcome(State.RECONCILED, NEW_DOC, 1, "x", erp_id=f"{TEST_CO[1]}-00000001"),
    PostOutcome(State.VALIDATION_REJECTED, NEW_DOC, 1, "x", problems=(("unsupported_voucher_type", "x"),)),
    PostOutcome(State.TALLY_REJECTED, NEW_DOC, 1, "x", tally_errors=("e",)),
    PostOutcome(State.OUTCOME_UNCERTAIN, NEW_DOC, 1, "x" * 5000, sent=True),
    PostOutcome(State.BLOCKED, NEW_DOC, 1, "x", problems=(("reconciliation_required", "x"),)),
], ids=lambda o: o.state)
def test_reported_results_are_exactly_person3_erp_result_keywords(outcome):
    allowed, required = _p3_erp_result_keywords()
    result = outcome.erp_result()
    caller_supplied = {"job_id", "revision", "idempotency_key"}  # from the claimed job, by the ResultSink
    assert set(result) <= allowed and required - caller_supplied <= set(result)
    assert result["ok"] is (outcome.state in SUCCESS) and (result["erp_id"] is not None) is result["ok"]
    assert result["error"] is None or len(result["error"]) <= 1000


MALFORMED_JOBS = {
    "none": None,
    "list": [],
    "text": "purchase",
    "draft_is_a_list": {"document_id": NEW_DOC, "voucher_draft": ["purchase"]},
    "draft_is_text": {"document_id": NEW_DOC, "voucher_draft": "purchase"},
    "voucher_type_is_a_list": {"document_id": NEW_DOC, "voucher_draft": {"voucher_type": ["purchase"]}},
    "voucher_type_is_a_dict": {"document_id": NEW_DOC, "voucher_draft": {"voucher_type": {"purchase": 1}}},
}


@pytest.mark.parametrize("method", ["post_invoice", "post_credit_debit", "post_payment"])
@pytest.mark.parametrize("job", MALFORMED_JOBS.values(), ids=list(MALFORMED_JOBS))
def test_malformed_jobs_are_structured_refusals_never_exceptions(tally, tmp_path, method, job):
    fake = tally()
    out = getattr(TallyConnector(paired(fake, tmp_path)), method)(job, approval(make_job()))
    assert isinstance(out, PostOutcome) and out.state == State.NOT_AUTHORIZED
    assert "unsupported_voucher_type" in dict(out.problems) and out.erp_result() is None
    assert fake.kinds == [] and not (tmp_path / "state").exists()


def test_a_malformed_draft_with_a_genuine_approval_is_reported_as_rejected(tally, tmp_path):
    fake = tally()
    j = make_job()
    j["voucher_draft"] = ["not", "a", "draft"]
    out = TallyConnector(paired(fake, tmp_path)).post_invoice(j, approval(j))
    assert out.state == State.VALIDATION_REJECTED and "unsupported_voucher_type" in dict(out.problems)
    assert out.erp_result()["ok"] is False and fake.kinds == []


def _auth_problems(j):
    return {
        "no_approval_record": (None, "not_authorized"),
        "approval_of_another_document": (approval(make_job(doc=DOC_B)), "document_mismatch"),
        "edited_after_approval": (approval(j, attempt_request_hash="0" * 64), "job_changed"),
        "payment_release_only": (approval(j, approvals=(("payment_release", 1),)), "approval_missing"),
        "already_posted_attempt": (approval(j, attempt_result="posted"), "not_pending"),
    }


@pytest.mark.parametrize("method,draft", [("post_invoice", {"voucher_type": "payment"}),
                                          ("post_invoice", {"voucher_type": "credit_note"}),
                                          ("post_credit_debit", {"voucher_type": "debit_note"}),
                                          ("post_payment", {"voucher_type": "payment"})])
@pytest.mark.parametrize("case", ["no_approval_record", "approval_of_another_document", "edited_after_approval",
                                  "payment_release_only", "already_posted_attempt"])
def test_an_unsupported_job_without_a_valid_approval_is_never_reported(tally, tmp_path, method, draft, case):
    fake = tally()
    j = make_job(**draft)
    auth, code = _auth_problems(j)[case]
    out = getattr(TallyConnector(paired(fake, tmp_path)), method)(j, auth)
    assert out.state == State.NOT_AUTHORIZED and out.erp_result() is None
    assert {code, "unsupported_voucher_type"} <= set(dict(out.problems)) and fake.kinds == []


@pytest.mark.parametrize("method", ["post_invoice", "post_credit_debit", "post_payment"])
def test_with_posting_disabled_no_post_method_reports_anything(tally, tmp_path, method):
    fake = tally()
    j = make_job(voucher_type="payment" if method != "post_invoice" else "purchase")
    out = getattr(TallyConnector(paired(fake, tmp_path, posting_enabled=False)), method)(j, approval(j))
    assert out.state == State.NOT_AUTHORIZED and "posting_disabled" in dict(out.problems)
    assert out.erp_result() is None and fake.kinds == [] and fake.imports == []


@pytest.mark.parametrize("changes,code", [({"state_dir": Path("finledger_tally_state")}, "state_dir_not_absolute"),
                                          ({"client_id": None}, "not_paired")])
@pytest.mark.parametrize("method", ["post_invoice", "post_credit_debit", "post_payment"])
def test_post_methods_refuse_a_configuration_the_agent_would_refuse(tally, tmp_path, monkeypatch, changes, code,
                                                                     method):
    monkeypatch.chdir(tmp_path)
    fake = tally()
    j = make_job()
    out = getattr(TallyConnector(paired(fake, tmp_path, **changes)), method)(j, approval(j))
    assert out.state == State.NOT_AUTHORIZED and code in dict(out.problems) and out.erp_result() is None
    assert fake.kinds == [] and not (tmp_path / "finledger_tally_state").exists()


def test_an_unsupported_voucher_type_is_quoted_bounded(tally, tmp_path):
    fake = tally()
    j = make_job(voucher_type=SECRET_LEDGER * 300)
    out = TallyConnector(paired(fake, tmp_path)).post_invoice(j, approval(j))
    assert out.state == State.VALIDATION_REJECTED and len(out.detail) < 120 and fake.kinds == []


# ====================== agent: construction, bounds, one job at a time ======================


@pytest.mark.parametrize("changes", [{"state_dir": Path("finledger_tally_state")}, {"state_dir": "rel"},
                                     {"client_id": None}, {"company_guid": None}, {"company_name": None}])
def test_an_agent_cannot_be_built_for_an_unsafe_posting_configuration(tally, tmp_path, monkeypatch, changes):
    monkeypatch.chdir(tmp_path)
    fake = tally()
    src = Source([claimed()])
    with pytest.raises(ValueError, match="refusing to build a posting agent"):
        AgentHost(settings_for(fake, tmp_path, **changes), src, Sink())
    assert src.claims == 0 and fake.kinds == [] and not (tmp_path / "finledger_tally_state").exists()


def test_a_non_posting_agent_may_use_a_relative_state_folder(tally, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path, posting_enabled=False, state_dir=Path("st")), Source([]), Sink())
    assert host.status()["not_ready_reason"] == "not_started"


@pytest.mark.parametrize("kw", [{"interval": float("nan")}, {"interval": float("inf")}, {"interval": -1},
                                {"interval": 0}, {"interval": True}, {"interval": "1"}, {"interval": 1e300},
                                {"interval": 86401}, {"interval": 0.01, "max_ticks": 0},
                                {"interval": 0.01, "max_ticks": -1}, {"interval": 0.01, "max_ticks": True},
                                {"interval": 0.01, "max_ticks": 1.5}, {"interval": 0.01, "max_ticks": "2"}])
def test_run_refuses_an_unbounded_or_nonsense_schedule(tally, tmp_path, kw):
    fake = tally()
    src = Source([claimed()])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    with pytest.raises(ValueError):
        host.run(**kw)
    assert src.claims == 0 and fake.kinds == [] and not (tmp_path / "state" / "agent.lock").exists()


def _downs_tally_on_first_claim(fake, item):
    class Downer(Repeat):
        def claim(self):
            fake.down = True
            return super().claim()
    return Downer(item)


ERROR_LOOPS = {
    "claim_fails": lambda fake: (Repeat(error=ConnectionError("platform down")), Sink(), "claim failed"),
    "invalid_claim": lambda fake: (Repeat({"not": "a claim"}), Sink(), "invalid claim"),
    "post_raises": lambda fake: (Repeat(claimed()), Sink(), "RuntimeError"),
    "report_fails": lambda fake: (Repeat(claimed()), Sink(fail=True), "report failed"),
    "send_uncertain_then_blocked": lambda fake: (Repeat(claimed()), Sink(), None),
    "tally_goes_down": lambda fake: (_downs_tally_on_first_claim(fake, claimed()), Sink(), "not ready"),
}


@pytest.mark.parametrize("name", ERROR_LOOPS, ids=list(ERROR_LOOPS))
def test_a_job_that_keeps_failing_waits_the_full_interval_every_time(tally, tmp_path, monkeypatch, name):
    fake = tally(import_mode="hangup_before_create" if name == "send_uncertain_then_blocked" else "create")
    source, sink, label = ERROR_LOOPS[name](fake)
    host = AgentHost(settings_for(fake, tmp_path), source, sink)
    if name == "post_raises":
        def explode(*a, **k):
            raise RuntimeError("bug")
        monkeypatch.setattr(host.connector, "post_invoice", explode)
    starts: list[float] = []
    real_tick = host.tick

    def timed_tick():
        starts.append(time.monotonic())
        return real_tick()
    host.tick = timed_tick
    interval = 0.05
    results = host.run(interval=interval, max_ticks=5)
    assert len(results) == 5 and len(fake.imports) <= 1
    assert all(b - a >= interval * 0.9 for a, b in zip(starts, starts[1:])), starts
    assert not any(r.outcome is not None and r.outcome.state == State.POSTED for r in results[1:])
    if label:
        assert label in (host.status()["last_error"] or "")
    else:
        assert [r.outcome.state for r in results] == [State.OUTCOME_UNCERTAIN] + [State.BLOCKED] * 4


def test_two_threads_ticking_still_handle_one_job_at_a_time(tally, tmp_path):
    fake = tally()
    src = Source([claimed(n, doc=d) for n, d in enumerate((NEW_DOC, DOC_B, DOC_C, DOC_D), 1)])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    host.startup()
    active, peak, lock = [0], [0], threading.Lock()
    real = host.connector.post_invoice

    def slow_post(*a, **k):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.05)
            return real(*a, **k)
        finally:
            with lock:
                active[0] -= 1
    host.connector.post_invoice = slow_post
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(host.tick())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak[0] == 1 and len(fake.imports) == 4
    assert sorted(r.outcome.state for r in results) == [State.POSTED] * 4 and host.status()["ticks"] == 4


def test_the_instance_lock_refuses_a_second_agent_while_the_first_runs(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    first = AgentHost(s, Source([]), Sink())
    runner = threading.Thread(target=first.run, kwargs={"interval": 30})
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while first.status()["ticks"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        src = Source([claimed()])
        with pytest.raises(AgentAlreadyRunning):
            AgentHost(s, src, Sink()).run(interval=0.01, max_ticks=1)
        assert src.claims == 0 and fake.imports == []
    finally:
        first.stop.set()
        runner.join(10)
    assert AgentHost(s, Source([]), Sink()).run(interval=0.01, max_ticks=1)  # released once the first stopped


class _ProcessDeath(BaseException):
    """Stands in for an exit the loop must not swallow (SystemExit, KeyboardInterrupt)."""


def test_the_lock_is_released_when_run_dies(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    with pytest.raises(_ProcessDeath):
        AgentHost(s, Repeat(error=_ProcessDeath()), Sink()).run(interval=0.01, max_ticks=3)
    assert len(AgentHost(s, Source([]), Sink()).run(interval=0.01, max_ticks=1)) == 1 and fake.imports == []


# ====================== agent: health recovery, status, diagnostics ======================


def test_health_recovers_after_an_ambiguous_company_is_closed(tally, tmp_path):
    fake = tally()
    src = Source([claimed(1), claimed(2, doc=DOC_B)])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    host.startup()
    fake.companies.append(CASE_TWIN)  # someone opens a same-named company while the agent runs
    first = host.tick()
    assert first.outcome.state == State.BLOCKED and "company_unverified" in dict(first.outcome.problems)
    second = host.tick()  # health is re-checked before claiming: still ambiguous, nothing claimed
    assert not second.claimed and src.claims == 1 and host.status()["not_ready_reason"] == Health.COMPANY_AMBIGUOUS
    fake.companies.remove(CASE_TWIN)
    third = host.tick()
    assert third.outcome.state == State.POSTED and host.status()["not_ready_reason"] is None
    assert len(fake.imports) == 1


def test_a_refused_import_connection_rechecks_health_before_the_next_claim(tally, tmp_path, monkeypatch):
    fake = tally()

    def refused(self, body):
        fake.down = True
        raise TallyUnavailable("simulated: connection refused before any byte was sent")
    monkeypatch.setattr(TallyTransport, "import_vouchers", refused)
    src = Source([claimed(1), claimed(2, doc=DOC_B)])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    host.startup()
    first = host.tick()
    assert first.outcome.state == State.SEND_NOT_DELIVERED and host.status()["health_recheck_pending"] is True
    second = host.tick()
    assert not second.claimed and src.claims == 1 and fake.imports == []


@pytest.mark.parametrize("setup,reason", [
    (lambda fake, tmp: ({"posting_enabled": False}, None), "posting_disabled"),
    (lambda fake, tmp: ({}, setattr(fake, "down", True)), Health.TALLY_UNREACHABLE),
    (lambda fake, tmp: ({}, fake.companies.__setitem__(slice(None), [EXACT_TWIN])), Health.COMPANY_GUID_MISMATCH),
    (lambda fake, tmp: ({}, fake.companies.append(CASE_TWIN)), Health.COMPANY_AMBIGUOUS),
    (lambda fake, tmp: ({}, fake.companies.__setitem__(slice(None), [])), Health.NO_COMPANY_LOADED),
    (lambda fake, tmp: ({}, fake.ledgers.__setitem__(TEST_CO[1], [])), "masters_unavailable"),
    (lambda fake, tmp: ({}, None), None),
], ids=["posting_disabled", "tally_down", "guid_mismatch", "ambiguous", "no_company", "masters", "ready"])
def test_status_names_one_clear_reason_when_not_ready(tally, tmp_path, setup, reason):
    fake = tally()
    changes, _ = setup(fake, tmp_path)
    src = Source([claimed()])
    host = AgentHost(settings_for(fake, tmp_path, **changes), src, Sink())
    assert host.status()["not_ready_reason"] == "not_started"
    host.run(interval=0.01, max_ticks=1)
    st = host.status()
    assert st["not_ready_reason"] == reason and st["ready_to_post"] is (reason is None)
    assert src.claims == (1 if reason is None else 0)
    if reason == "posting_disabled":
        assert st["posting"] == "disabled" and st["settings"]["posting_enabled"] is False
    assert TEST_CO[0] not in json.dumps(st) and TEST_CO[0].upper() not in json.dumps(st)


@pytest.mark.parametrize("bad_report", [None, {"status": "company_verified", "posting": "enabled"}])
def test_a_health_check_that_returns_garbage_means_not_ready(tally, tmp_path, bad_report):
    fake = tally()
    src = Source([claimed()])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    host.connector.health = lambda **kw: bad_report
    assert host.run(interval=0.01, max_ticks=2) == [] and src.claims == 0
    assert host.status()["not_ready_reason"] == "health_check_failed" and host.ready_to_post is False


class _Exploding(PostOutcome):
    def erp_result(self):
        raise RuntimeError(f"bug while building the result for {SECRET_LEDGER}")


@pytest.mark.parametrize("returned", [None, {"ok": True, "erp_id": "g"}, "posted",
                                      _Exploding(State.POSTED, NEW_DOC, 1, "x", erp_id="g")],
                         ids=["none", "dict", "text", "erp_result_raises"])
def test_a_connector_returning_garbage_is_an_error_never_a_report(tally, tmp_path, monkeypatch, returned):
    fake = tally()
    sink = Sink()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed(1), claimed(2, doc=DOC_B)]), sink)
    monkeypatch.setattr(host.connector, "post_invoice", lambda *a, **k: returned)
    results = host.run(interval=0.01, max_ticks=2)
    assert len(results) == 2 and all(r.error and r.outcome is None and not r.reported for r in results)
    assert sink.reports == [] and host.status()["by_state"] == {"error": 2}
    assert SECRET_LEDGER not in json.dumps(host.status())


def test_status_and_logs_carry_labels_not_messages_or_client_data(tally, tmp_path, monkeypatch, caplog):
    fake = tally()
    caplog.set_level(logging.DEBUG, logger="finledger_tally")
    j = make_job()
    j["document_id"] = SECRET_LEDGER.replace(" ", "")  # a malformed id that looks like a ledger name
    bad = ClaimedJob("x" * 5000, j, approval(make_job()))
    host = AgentHost(settings_for(fake, tmp_path), Source([bad, claimed(2)]), Sink())

    def explode(*a, **k):
        raise RuntimeError(f"{SECRET_LEDGER} {TEST_CO[0]} " * 50)
    host.startup()
    monkeypatch.setattr(host.connector, "post_invoice", explode)
    first = host.tick()
    monkeypatch.undo()
    second = host.tick()
    assert first.error.startswith("RuntimeError") and second.outcome.state == State.POSTED
    st = json.dumps(host.status())
    assert host.status()["last_error"] == "RuntimeError" and SECRET_LEDGER not in st and TEST_CO[0] not in st
    lines = [r.getMessage() for r in caplog.records]
    assert lines and all(len(t) <= 400 for t in lines)
    joined = "\n".join(lines)
    for secret in (SECRET_LEDGER, SECRET_LEDGER.replace(" ", ""), TEST_CO[0], "x" * 100):
        assert secret not in joined


# ====================== restart safety (new cases) ======================


def test_restart_after_send_started_could_not_be_recorded_posts_exactly_once(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)
    sink = Sink()
    with monkeypatch.context() as m:
        fail_on(m, Phase.SEND_STARTED)  # the disk fails just before the send would start
        first = AgentHost(s, Source([claimed(1)]), sink).run(interval=0.01, max_ticks=1)
    assert first[0].outcome.state == State.BLOCKED and "journal_write_failed" in dict(first[0].outcome.problems)
    assert fake.imports == [] and sink.reports == []  # nothing sent, nothing reported: the job stays approved
    second = AgentHost(s, Source([claimed(1)]), sink).run(interval=0.01, max_ticks=1)
    assert second[0].outcome.state == State.POSTED and len(fake.imports) == 1
    third = AgentHost(s, Source([claimed(1)]), sink).run(interval=0.01, max_ticks=1)
    assert third[0].outcome.state == State.ALREADY_POSTED and len(fake.imports) == 1
    assert [r[1]["erp_id"] for r in sink.reports] == [second[0].outcome.erp_id] * 2


def test_restart_after_a_failed_report_reports_the_same_voucher_without_an_import(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    first = AgentHost(s, Source([claimed(1)]), Sink(fail=True)).run(interval=0.01, max_ticks=1)
    assert first[0].outcome.state == State.POSTED and first[0].error.startswith("report failed")
    sink = Sink()
    second = AgentHost(s, Source([claimed(1)]), sink).run(interval=0.01, max_ticks=1)
    assert second[0].outcome.state == State.ALREADY_POSTED and second[0].reported
    assert sink.reports == [(1, {"ok": True, "erp_id": first[0].outcome.erp_id, "error": None})]
    assert len(fake.imports) == 1


def test_restart_with_a_torn_journal_blocks_and_never_reports_success(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    with open(s.journal_dir / NEW_DOC / "journal.jsonl", "ab") as f:
        f.write(b'{"seq": 99, "pha')  # power lost in the middle of an append
    fake.import_mode = "create"
    sink = Sink()
    again = AgentHost(s, Source([claimed(1)]), sink).run(interval=0.01, max_ticks=1)
    assert again[0].outcome.state == State.BLOCKED and "journal_unreadable" in dict(again[0].outcome.problems)
    assert [r[1]["ok"] for r in sink.reports] == [False] and len(fake.imports) == 1


def test_restart_when_the_state_folder_cannot_be_used_claims_nothing(tally, tmp_path):
    fake = tally()
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("not a folder")
    src = Source([claimed(1)])
    with pytest.raises(OSError):
        AgentHost(settings_for(fake, tmp_path, state_dir=blocker), src, Sink()).run(interval=0.01, max_ticks=1)
    assert src.claims == 0 and fake.kinds == [] and blocker.read_text() == "not a folder"
    fixed = AgentHost(settings_for(fake, tmp_path), Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert fixed[0].outcome.state == State.POSTED and len(fake.imports) == 1


def test_restart_while_tally_is_down_claims_nothing_and_keeps_the_block(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    first = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert first[0].outcome.state == State.OUTCOME_UNCERTAIN and len(fake.imports) == 1
    fake.down, fake.import_mode = True, "create"
    src = Source([claimed(1)])
    down = AgentHost(s, src, Sink())
    assert down.run(interval=0.01, max_ticks=3) == [] and src.claims == 0
    assert down.status()["not_ready_reason"] == Health.TALLY_UNREACHABLE
    fake.down = False
    back = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert back[0].outcome.state == State.BLOCKED and "reconciliation_required" in dict(back[0].outcome.problems)
    assert len(fake.imports) == 1


def test_restart_after_an_undelivered_send_posts_once_when_tally_is_back(tally, tmp_path, monkeypatch):
    fake = tally()
    s = settings_for(fake, tmp_path)

    def refused(self, body):
        raise TallyUnavailable("simulated: connection refused before any byte was sent")
    with monkeypatch.context() as m:
        m.setattr(TallyTransport, "import_vouchers", refused)
        first = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert first[0].outcome.state == State.SEND_NOT_DELIVERED and first[0].outcome.erp_result() is None
    fake.down = True
    src = Source([claimed(1)])
    assert AgentHost(s, src, Sink()).run(interval=0.01, max_ticks=1) == [] and src.claims == 0
    fake.down = False
    back = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert back[0].outcome.state == State.POSTED and len(fake.imports) == 1


def _malformed_claims():
    draft_list = make_job()
    draft_list["voucher_draft"] = ["purchase"]
    odd_id = make_job()
    odd_id["document_id"] = SECRET_LEDGER
    return {
        "draft_is_a_list": ClaimedJob(1, draft_list, approval(draft_list)),
        "payload_not_a_mapping": ClaimedJob(1, ["purchase"], approval(make_job())),
        "authorization_not_rows": ClaimedJob(1, make_job(), {"status": "approved"}),
        "document_id_not_canonical": ClaimedJob(1, odd_id, approval(odd_id)),
        "not_a_claimed_job": {"job_id": 1, "payload": make_job()},
        "voucher_type_garbage": ClaimedJob(1, make_job(voucher_type={"purchase": True}),
                                           approval(make_job(voucher_type={"purchase": True}))),
    }


@pytest.mark.parametrize("name", list(_malformed_claims()))
def test_restart_with_a_malformed_job_never_imports_it_or_reports_it_as_success(tally, tmp_path, name):
    fake = tally()
    s = settings_for(fake, tmp_path)
    good = claimed(2, doc=DOC_B)
    states = []
    sinks = []
    for _ in range(2):  # the same deliveries to two processes on the same state folder
        sink = Sink()
        results = AgentHost(s, Source([_malformed_claims()[name], good]), sink).run(interval=0.01, max_ticks=2)
        assert len(results) == 2
        bad = results[0]
        assert bad.outcome is None or bad.outcome.state not in SUCCESS
        assert bad.error or bad.outcome.state in (State.NOT_AUTHORIZED, State.VALIDATION_REJECTED)
        states.append(results[1].outcome.state)
        sinks.append(sink)
    assert states == [State.POSTED, State.ALREADY_POSTED] and len(fake.imports) == 1
    for sink in sinks:
        assert all(not report["ok"] for job_id, report in sink.reports if job_id == 1)


def test_a_stop_signal_during_an_in_flight_import_finishes_and_journals_only_that_job(tmp_path):
    fake = InFlightTally(slow_seconds=0.6)
    try:
        _stop_during_import(fake, tmp_path)
    finally:
        fake.close()


def _stop_during_import(fake, tmp_path):
    s = settings_for(fake, tmp_path)
    src = Source([claimed(1), claimed(2, doc=DOC_B)])
    host = AgentHost(s, src, Sink())
    old = signal.getsignal(signal.SIGTERM)
    try:
        stop_on_signals(host, (signal.SIGTERM,))
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not old

        def signal_once_the_import_is_in_flight():  # call the installed handler: no real signal hits pytest
            if fake.import_started.wait(10):
                handler(signal.SIGTERM, None)
        threading.Thread(target=signal_once_the_import_is_in_flight, daemon=True).start()
        results = host.run(interval=60)
    finally:
        signal.signal(signal.SIGTERM, old)
    assert [r.outcome.state for r in results] == [State.POSTED] and src.claims == 1 and len(fake.imports) == 1
    assert phases(s)[-1] == Phase.POSTED.value and host.status()["stopped"] is True
    restarted = AgentHost(s, Source([claimed(1)]), Sink()).run(interval=0.01, max_ticks=1)
    assert restarted[0].outcome.state == State.ALREADY_POSTED and len(fake.imports) == 1


def test_status_after_restart_is_this_process_and_the_journal_decides(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")
    s = settings_for(fake, tmp_path)
    first = AgentHost(s, Source([claimed(1)]), Sink())
    first.run(interval=0.01, max_ticks=1)
    assert first.status()["by_state"] == {State.OUTCOME_UNCERTAIN: 1}
    fake.import_mode = "create"
    second = AgentHost(s, Source([claimed(1), claimed(2, doc=DOC_B)]), Sink())
    fresh = second.status()
    assert (fresh["ticks"], fresh["ready_to_post"], fresh["not_ready_reason"], fresh["started_at"]) == \
        (0, False, "not_started", None)
    second.run(interval=0.01, max_ticks=2)
    st = second.status()
    assert st["by_state"] == {State.BLOCKED: 1, State.POSTED: 1} and st["ticks"] == 2
    assert st["ready_to_post"] is True and st["not_ready_reason"] is None and st["started_at"]
    assert st["last_error"] is None and st["last_state"] == State.POSTED and len(fake.imports) == 2
    assert TEST_CO[0] not in json.dumps(st) and "TEST-INV-001" not in json.dumps(st)


# ---------- sync: read-only snapshots to person2 ----------


class _Platform:
    """A person2 stand-in on 127.0.0.1 that records each request and answers ``status`` (or a redirect)."""

    def __init__(self, status=201, redirect_to=None):
        import http.server
        self.requests, platform = [], self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                platform.requests.append((self.path, self.headers.get("Authorization"), json.loads(body)))
                if redirect_to:
                    self.send_response(307)
                    self.send_header("Location", redirect_to + self.path)
                else:
                    self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def platform():
    made = []

    def start(**kw):
        made.append(_Platform(**kw))
        return made[-1]
    yield start
    for p in made:
        p.close()


TOKEN = "agent-token-not-for-output"


def test_sync_sends_masters_and_open_bills_with_the_agent_token_and_never_imports(tally, platform, monkeypatch):
    calls = never_imports(monkeypatch)
    fake, p2 = tally(), platform()
    code, text = cli(["sync"], _env(fake, FINLEDGER_PLATFORM_URL=p2.url, FINLEDGER_AGENT_TOKEN=TOKEN))
    body = json.loads(text)
    assert (code, body["ok"], body["reason"]) == (0, True, "ok")
    paths = [path for path, _, _ in p2.requests]
    assert paths[-1] == "/agent/open-bills" and set(paths[:-1]) == {"/agent/masters"} and len(paths) >= 2
    assert {auth for _, auth, _ in p2.requests} == {f"Bearer {TOKEN}"}
    assert calls == [] and fake.imports == [] and "import" not in fake.kinds
    for secret in (TOKEN, TEST_CO[0], OTHER_CO[0]):
        assert secret not in text


def test_sync_reports_a_rejected_snapshot_as_failed(tally, platform):
    fake, p2 = tally(), platform(status=422)
    code, text = cli(["sync"], _env(fake, FINLEDGER_PLATFORM_URL=p2.url, FINLEDGER_AGENT_TOKEN=TOKEN))
    body = json.loads(text)
    assert (code, body["reason"]) == (2, "failed") and {r["http_status"] for r in body["results"]} == {422}


def test_sync_sends_nothing_when_tally_cannot_be_read(tally, platform):
    fake, p2 = tally(), platform()
    fake.down = True
    code, text = cli(["sync"], _env(fake, FINLEDGER_PLATFORM_URL=p2.url, FINLEDGER_AGENT_TOKEN=TOKEN))
    assert (code, json.loads(text)["reason"]) == (2, "failed") and p2.requests == []


def test_sync_never_forwards_the_token_through_a_redirect(tally, platform):
    fake, elsewhere = tally(), platform()
    p2 = platform(redirect_to=elsewhere.url)
    code, _ = cli(["sync"], _env(fake, FINLEDGER_PLATFORM_URL=p2.url, FINLEDGER_AGENT_TOKEN=TOKEN))
    assert code == 2 and p2.requests and elsewhere.requests == []


@pytest.mark.parametrize("url,token", [("", TOKEN), ("http://platform.example.com", TOKEN),
                                       ("ftp://127.0.0.1", TOKEN), ("https://u:p@platform.example.com", TOKEN),
                                       ("https://platform.example.com", ""), ("https://platform.example.com", " padded-secret-token")])
def test_sync_refuses_an_unsafe_or_incomplete_platform_setting(tally, url, token):
    fake = tally()
    code, text = cli(["sync"], _env(fake, FINLEDGER_PLATFORM_URL=url, FINLEDGER_AGENT_TOKEN=token))
    assert (code, json.loads(text)["reason"]) == (1, "invalid_configuration") and fake.kinds == []
    assert not token.strip() or token not in text
