"""Agent host lifecycle and the read-only CLI. SYNTHETIC Tally only; nothing here talks to a real Tally."""

from __future__ import annotations

import io
import json

import pytest

from finledger_tally import AgentAlreadyRunning, AgentHost, InstanceLock, State
from finledger_tally.__main__ import main
from test_hardening import Sink, Source, claimed
from test_isolation_check import TEST_CO
from test_posting import CLIENT, settings_for, tally  # noqa: F401 (fixture)


def test_a_second_agent_on_the_same_state_cannot_start(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    with InstanceLock(s.state_dir):
        with pytest.raises(AgentAlreadyRunning):
            AgentHost(s, Source([claimed()]), Sink()).run(interval=0.01, max_ticks=1)
    assert fake.imports == []
    # released: the next agent starts
    assert len(AgentHost(s, Source([]), Sink()).run(interval=0.01, max_ticks=1)) == 1


def test_restart_after_an_uncertain_send_never_resends(tally, tmp_path):
    fake = tally(import_mode="hangup_before_create")  # the connection drops after the request: outcome unknown
    s = settings_for(fake, tmp_path)
    c = claimed()
    first = AgentHost(s, Source([c]), Sink()).run(interval=0.01, max_ticks=1)
    assert first[0].outcome.state == State.OUTCOME_UNCERTAIN and len(fake.imports) == 1
    fake.import_mode = "create"  # Tally is healthy again; a naive retry would now create the voucher
    second = AgentHost(s, Source([c]), Sink()).run(interval=0.01, max_ticks=1)  # a new process, same state dir
    assert second[0].outcome.state == State.BLOCKED and len(fake.imports) == 1
    assert "reconciliation_required" in dict(second[0].outcome.problems)


def test_restart_after_posting_returns_the_same_voucher(tally, tmp_path):
    fake = tally()
    s = settings_for(fake, tmp_path)
    c = claimed()
    AgentHost(s, Source([c]), Sink()).run(interval=0.01, max_ticks=1)
    again = AgentHost(s, Source([c]), Sink()).run(interval=0.01, max_ticks=1)
    assert again[0].outcome.state == State.ALREADY_POSTED and len(fake.imports) == 1


def test_a_failing_health_check_means_not_ready(tally, tmp_path, monkeypatch):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed()]), Sink())

    def broken(*a, **k):
        raise RuntimeError("bug in health")
    monkeypatch.setattr(host.connector, "health", broken)
    assert host.run(interval=0.01, max_ticks=2) == [] and host.source.claims == 0
    assert host.status()["ready_to_post"] is False and "health check failed" in host.status()["last_error"]


def test_health_is_checked_again_after_tally_goes_away(tally, tmp_path):
    fake = tally()
    src = Source([claimed(1), claimed(2)])
    host = AgentHost(settings_for(fake, tmp_path), src, Sink())
    host.startup()
    fake.down = True
    assert host.tick().outcome.state == State.TALLY_UNAVAILABLE
    second = host.tick()  # health re-checked first: Tally still down, so nothing is claimed
    assert not second.claimed and src.claims == 1 and host.status()["tally"] == "tally_unreachable"
    fake.down = False
    third = host.tick()
    assert third.claimed and third.outcome.state == State.POSTED and host.status()["tally"] == "company_verified"


def test_status_is_structured_and_carries_no_company_name(tally, tmp_path):
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed()]), Sink())
    host.run(interval=0.01, max_ticks=2)
    st = host.status()
    assert st["by_state"] == {"idle": 1, State.POSTED: 1} and st["ticks"] == 2 and st["ready_to_post"] is True
    assert TEST_CO[0] not in json.dumps(st) and st["settings"]["posting_enabled"] is True


def test_posting_is_off_for_an_agent_built_from_an_empty_environment(tmp_path):
    from finledger_tally import AgentSettings
    assert AgentSettings.from_env({}).posting_enabled is False
    assert AgentSettings.from_env({"FINLEDGER_TALLY_POSTING": "true"}).posting_enabled is False
    assert AgentSettings.from_env({"FINLEDGER_TALLY_POSTING": "Enabled"}).posting_enabled is False


# ---------- CLI ----------


def run_cli(args, env):
    out = io.StringIO()
    code = main(args, env, out)
    return code, out.getvalue()


def test_cli_config_is_redacted():
    code, text = run_cli(["config"], {"FINLEDGER_TALLY_COMPANY": "Secret Traders Pvt Ltd",
                                      "FINLEDGER_TALLY_CLIENT_ID": CLIENT})
    assert code == 0 and "Secret Traders" not in text and json.loads(text)["settings"]["company_name_set"] is True


@pytest.mark.parametrize("env", [{"FINLEDGER_TALLY_PORT": "abc"}, {"FINLEDGER_TALLY_COMPANY_GUID": "not-a-guid"},
                                 {"FINLEDGER_TALLY_CLIENT_ID": "x"}, {"FINLEDGER_TALLY_PORT": "0"}])
def test_cli_rejects_invalid_configuration(env):
    code, text = run_cli(["config"], env)
    assert code == 1 and json.loads(text)["ok"] is False


def test_cli_health_exit_codes(tally):
    fake = tally()
    env = {"FINLEDGER_TALLY_COMPANY": TEST_CO[0], "FINLEDGER_TALLY_COMPANY_GUID": TEST_CO[1],
           "FINLEDGER_TALLY_PORT": str(fake.port), "FINLEDGER_TALLY_TIMEOUT": "3"}
    code, text = run_cli(["health"], env)
    assert code == 0 and json.loads(text)["health"]["status"] == "company_verified"
    assert fake.imports == []
    fake.down = True
    code, text = run_cli(["health"], env)
    assert code == 2 and json.loads(text)["ok"] is False


@pytest.mark.parametrize("args", [[], ["run"], ["post"], ["health", "extra"]])
def test_cli_has_no_posting_command(args):
    assert run_cli(args, {})[0] == 64


def test_a_stop_signal_ends_the_loop_after_the_current_job(tally, tmp_path):
    import os
    import signal
    import threading

    from finledger_tally.agent import stop_on_signals
    fake = tally()
    host = AgentHost(settings_for(fake, tmp_path), Source([claimed()]), Sink())
    old = signal.getsignal(signal.SIGTERM)
    try:
        stop_on_signals(host, (signal.SIGTERM,))
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        results = host.run(interval=60)
    finally:
        signal.signal(signal.SIGTERM, old)
    assert len(results) == 1 and results[0].outcome.state == State.POSTED and host.stop.is_set()
