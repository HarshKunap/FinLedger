"""``python -m finledger_tally preflight FILE.json``: the read-only live-Import preflight on the CLI, against a SYNTHETIC
Tally only. The CLI can run the checks; it has no way to send the Import."""

from __future__ import annotations

import io
import json

import pytest

import finledger_tally.posting as posting
from finledger_tally import p3_request_hash
from finledger_tally.__main__ import main
from test_posting import CLIENT, make_job
from test_preflight import LIVE, OTHER, live_tally
from test_voucher_xml import PURCHASE, VENDOR


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


def rows(j, **changes):
    data = {"client_id": CLIENT, "job": j,
            "document": {"id": j["document_id"], "client_id": CLIENT, "status": "approved"},
            "attempt": {"client_id": CLIENT, "document_id": j["document_id"], "result": "pending",
                        "revision": j["revision"], "idempotency_key": j["idempotency_key"],
                        "request_hash": p3_request_hash(j), "erp_id": None},
            "approvals": [{"client_id": CLIENT, "document_id": j["document_id"], "kind": "human",
                           "revision": j["revision"], "superseded_at": None}]}
    data.update(changes)
    return data


def env_for(f, tmp_path, **extra):
    env = {"FINLEDGER_TALLY_COMPANY": LIVE[0], "FINLEDGER_TALLY_COMPANY_GUID": LIVE[1],
           "FINLEDGER_TALLY_PORT": str(f.port), "FINLEDGER_TALLY_TIMEOUT": "3", "FINLEDGER_TALLY_CLIENT_ID": CLIENT,
           "FINLEDGER_TALLY_STATE_DIR": str(tmp_path / "state"), "FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID": LIVE[1]}
    env.update(extra)
    return env


def run(tmp_path, data, env):
    path = tmp_path / "preflight.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    out = io.StringIO()
    code = main(["preflight", str(path)], env, out)
    return code, out.getvalue()


def never_imports(monkeypatch):
    calls = []
    monkeypatch.setattr(posting, "post_approved", lambda *a, **k: calls.append(a))
    return calls


def test_a_ready_preflight_exits_0_and_sends_nothing(fake, tmp_path, monkeypatch):
    calls = never_imports(monkeypatch)
    f = fake()
    code, text = run(tmp_path, rows(make_job()), env_for(f, tmp_path))
    body = json.loads(text)
    assert (code, body["ok"], body["reason"], body["failed"]) == (0, True, "ok", [])
    assert "READY" in body["summary"] and body["request_sha256"] in body["summary"]
    assert "IMPORT ONE VOUCHER" in body["summary"]
    assert f.imports == [] and "import" not in f.kinds and calls == []


def test_the_output_never_names_the_company_or_a_ledger(fake, tmp_path):
    f = fake()
    _, text = run(tmp_path, rows(make_job()), env_for(f, tmp_path))
    for secret in (LIVE[0], VENDOR, PURCHASE):
        assert secret not in text


@pytest.mark.parametrize("extra,check", [
    ({"FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID": ""}, "disposable_company_designated"),
    ({"FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID": OTHER[1]}, "disposable_company_designated"),
    ({"FINLEDGER_TALLY_POSTING": "enabled"}, "posting_state"),  # the dry run requires posting still off
])
def test_unmet_conditions_exit_2_with_the_failed_check(fake, tmp_path, extra, check):
    f = fake()
    code, text = run(tmp_path, rows(make_job()), env_for(f, tmp_path, **extra))
    body = json.loads(text)
    assert (code, body["ok"], body["reason"]) == (2, False, "not_ready") and check in body["failed"]
    assert "NOT READY" in body["summary"] and "IMPORT ONE VOUCHER" not in body["summary"]
    assert f.imports == []


def test_a_superseded_approval_is_not_ready(fake, tmp_path):
    f = fake()
    j = make_job()
    data = rows(j)
    data["approvals"][0]["superseded_at"] = "2026-10-11T10:00:00Z"
    code, text = run(tmp_path, data, env_for(f, tmp_path))
    body = json.loads(text)
    assert code == 2 and {"authorization", "human_approval"} <= set(body["failed"]) and f.imports == []


@pytest.mark.parametrize("data", ["not json", "[]", json.dumps({"job": "x"}),
                                  json.dumps({"job": {}, "client_id": CLIENT})])
def test_unusable_input_exits_1_before_reaching_tally(fake, tmp_path, data):
    f = fake()
    code, text = run(tmp_path, data, env_for(f, tmp_path))
    body = json.loads(text)
    assert (code, body["ok"], body["reason"]) == (1, False, "invalid_input") and f.kinds == []


def test_rows_of_another_client_are_invalid_input(fake, tmp_path):
    f = fake()
    j = make_job()
    data = rows(j)
    data["approvals"][0]["client_id"] = "44444444-4444-4444-8444-444444444444"
    code, text = run(tmp_path, data, env_for(f, tmp_path))
    assert (code, json.loads(text)["reason"]) == (1, "invalid_input") and f.kinds == []


def test_a_missing_file_is_invalid_input(tmp_path):
    out = io.StringIO()
    assert main(["preflight", str(tmp_path / "absent.json")], {}, out) == 1
    assert json.loads(out.getvalue())["reason"] == "invalid_input"


@pytest.mark.parametrize("args", [["preflight"], ["preflight", "a.json", "--execute"], ["preflight", "a.json", "--yes"],
                                  ["execute", "a.json"], ["import", "a.json"], ["preflight-execute", "a.json"]])
def test_there_is_no_execute_path_on_the_cli(args):
    out = io.StringIO()
    assert main(args, {}, out) == 64
