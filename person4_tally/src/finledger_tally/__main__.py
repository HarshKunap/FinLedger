"""Read-only operator commands. Nothing here posts or sends Import XML.

    python -m finledger_tally config   settings from the environment, redacted; exit 1 if they are invalid
    python -m finledger_tally health   one health check of the configured Tally company; exit 0 only if verified
    python -m finledger_tally sync     read ledger masters and open bills from the verified company (read-only) and send
                                       them to person2 (``POST /agent/masters``, ``POST /agent/open-bills``); exit 0
                                       only if every snapshot was accepted. Run it on a schedule (Task Scheduler).
    python -m finledger_tally preflight FILE.json
                                       the READ-ONLY live-Import preflight (docs/LIVE_IMPORT_TEST_PROCEDURE.md); exit 0
                                       only if every check passes. It never sends an Import: the CLI has no execute or
                                       post command and no flag that would add one.

There is deliberately no "run" command: the platform has no job-pickup route yet (docs/CONTRACT_GAPS.md #10), and the
agent must not run against an invented queue. ``AgentHost.run`` is the entry point a service will call once it exists.
Exit codes: 0 ok, 1 configuration invalid, 2 not ready (see the printed status), 3 unexpected internal error
(only the exception type is printed), 64 usage. ``health`` is ok only when the company is verified AND its ledger
masters were read.

Every output except the usage line (exit 64) is one JSON object with ``ok`` and a stable ``reason`` code, so a script
can tell the cases apart without parsing text: ``ok``; ``invalid_configuration`` (exit 1, with ``problems`` codes when
the settings parse but cannot run a posting agent: ``not_paired``, ``state_dir_not_absolute``); for exit 2 the
``Health`` status
(``tally_unreachable``, ``malformed_reply``, ``no_company_loaded``, ``company_not_configured``, ``company_unresolved``,
``company_ambiguous``, ``company_guid_mismatch``) or ``masters_failed``; ``internal_error`` (exit 3).
Exit 2 is deliberately one code for every not-ready reason (a service manager only needs ready / not ready, and the
reason is in the JSON); 1 stays "fix the configuration" and 3 "report a bug".

``sync`` needs ``FINLEDGER_PLATFORM_URL`` (https; plain http only to localhost) and ``FINLEDGER_AGENT_TOKEN`` (the
token ``/clients/{id}/erp/pair`` showed once). The token is never printed and redirects are refused, so it is never
forwarded to another host. Reasons: ``ok``, ``partial`` / ``failed`` (exit 2; each snapshot's result is listed),
``invalid_configuration`` (exit 1). A snapshot whose Tally read fails is not sent, so person2 keeps its last good copy.

``preflight`` reads FILE.json holding ``client_id``, ``job`` (the jobs.payload object), ``document``, ``attempt`` and
``approvals`` (the P2 rows ``PostAuthorization.from_rows`` takes) and ``FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID`` from
the environment. Reasons: ``ok`` (exit 0), ``not_ready`` (exit 2, with the failed check names), ``invalid_input``
(exit 1: the file is missing, not JSON, or its rows do not form one authorization). Its ``summary`` shows the invoice
reference and amount so the person can confirm the voucher; the configured company name is replaced by
``<company>``.

Nothing printed by ``config`` and ``health`` holds the company name, ledger names or reply text: settings are ``redacted()`` and the health report's
text fields carry exception type names and fixed phrases only. The client id and the resolved company GUID are
printed: they are identifiers, needed for pairing and support, not client data.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Mapping, Sequence

from .config import AgentSettings
from .connector import Health, TallyConnector
from .posting import PostAuthorization
from .preflight import live_import_preflight

EXIT_OK, EXIT_CONFIG, EXIT_NOT_READY, EXIT_INTERNAL, EXIT_USAGE = 0, 1, 2, 3, 64
_FLAGS = ("FINLEDGER_TALLY_POSTING", "FINLEDGER_TALLY_OFFLINE_XML")


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, out=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout

    def emit(code: int, body: dict) -> int:
        print(json.dumps(body, sort_keys=True), file=out)
        return code

    if not (len(argv) == 1 and argv[0] in ("config", "health", "sync") or len(argv) == 2 and argv[0] == "preflight"):
        print("usage: python -m finledger_tally {config|health|sync|preflight FILE.json}", file=out)
        return EXIT_USAGE
    try:
        settings = AgentSettings.from_env(env)
    except (ValueError, TypeError) as exc:
        return emit(EXIT_CONFIG, {"ok": False, "reason": "invalid_configuration",
                                  "error": f"invalid configuration: {_without_company(str(exc), env)}"})
    problems = settings.posting_problems()
    if problems:
        return emit(EXIT_CONFIG, {"ok": False, "reason": "invalid_configuration",
                                  "error": "invalid configuration: " + "; ".join(m for _, m in problems),
                                  "problems": [c for c, _ in problems], "settings": settings.redacted()})
    if argv[0] == "config":
        return emit(EXIT_OK, {"ok": True, "reason": "ok", "settings": settings.redacted(),
                              "warnings": _flag_warnings(env)})
    if argv[0] == "preflight":
        return _preflight(argv[1], settings, env, emit)
    if argv[0] == "sync":
        return _sync(settings, env, emit)
    try:
        report = TallyConnector(settings).health()
    except Exception as exc:  # noqa: BLE001 - never a traceback with request text; never "ok"
        return emit(EXIT_INTERNAL, {"ok": False, "reason": "internal_error",
                                    "error": f"unexpected {type(exc).__name__} during the health check"})
    ok = report.status == Health.COMPANY_VERIFIED and report.masters == "ok"
    reason = "ok" if ok else report.status if report.status != Health.COMPANY_VERIFIED else "masters_failed"
    return emit(EXIT_OK if ok else EXIT_NOT_READY, {"ok": ok, "reason": reason, "health": asdict(report),
                                                    "company_guid_checked": settings.company_guid is not None})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the bearer token must never follow a redirect to another host
        return None


def _platform_target(env: Mapping[str, str]) -> tuple[str, str] | None:
    url, token = (env.get("FINLEDGER_PLATFORM_URL") or "").rstrip("/"), env.get("FINLEDGER_AGENT_TOKEN") or ""
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if not token.strip() or token != token.strip() or not parts.hostname or parts.query or parts.fragment \
            or parts.username or not (parts.scheme == "https" or parts.scheme == "http" and local):
        return None
    return url, token


def _send(target: tuple[str, str], path: str, body: dict, timeout: float) -> int:
    request = urllib.request.Request(target[0] + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {target[1]}",
                                              "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:  # the body is not read: it could echo what was sent
        return exc.code


def _sync(settings: AgentSettings, env: Mapping[str, str] | None, emit) -> int:
    """Read-only towards Tally: masters and open bills of the verified company, sent to person2. Prints counts and
    HTTP status codes only (no company, ledger or bill names, never the token)."""
    target = _platform_target(os.environ if env is None else env)
    if target is None or not (settings.company_name and settings.company_guid):
        return emit(EXIT_CONFIG, {"ok": False, "reason": "invalid_configuration", "error": (
            "sync needs FINLEDGER_PLATFORM_URL (https, or http to localhost), FINLEDGER_AGENT_TOKEN, "
            "FINLEDGER_TALLY_COMPANY and FINLEDGER_TALLY_COMPANY_GUID")})
    connector, results = TallyConnector(settings), []

    def push(name: str, read, path: str) -> None:
        try:
            bodies = read()
        except Exception as exc:  # noqa: BLE001 - not sent: person2 keeps its last good snapshot
            results.append({"snapshot": name, "sent": False, "error": f"tally read failed: {type(exc).__name__}"})
            return
        for body in bodies:
            label = f"{name}:{body.get('kind')}" if name == "masters" else name
            items = len(body.get("items") or body.get("bills") or [])
            try:
                status = _send(target, path, body, settings.timeout)
            except Exception as exc:  # noqa: BLE001 - unreachable platform, TLS failure, timeout
                results.append({"snapshot": label, "items": items, "sent": False,
                                "error": f"platform unreachable: {type(exc).__name__}"})
                continue
            results.append({"snapshot": label, "items": items, "sent": status == 201, "http_status": status})

    push("masters", connector.masters_snapshot, "/agent/masters")
    push("open_bills", lambda: [connector.fetch_open_bills().platform_payload()], "/agent/open-bills")
    sent = sum(r["sent"] for r in results)
    reason = "ok" if sent == len(results) else "partial" if sent else "failed"
    return emit(EXIT_OK if reason == "ok" else EXIT_NOT_READY, {"ok": reason == "ok", "reason": reason,
                                                                "results": results})


def _preflight(path: str, settings: AgentSettings, env: Mapping[str, str] | None, emit) -> int:
    """Read-only: build the authorization from the rows in ``path`` and run ``live_import_preflight``. Never sends."""
    env = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("job"), dict):
            raise ValueError("the file must be a JSON object with a 'job' object")
        auth = PostAuthorization.from_rows(data.get("client_id"), data.get("document"), data.get("attempt"),
                                           data.get("approvals") or [])
    except (OSError, ValueError, TypeError, AttributeError) as exc:  # json.JSONDecodeError is a ValueError
        return emit(EXIT_CONFIG, {"ok": False, "reason": "invalid_input",
                                  "error": f"{type(exc).__name__}: {_without_company(str(exc), env)}"[:300]})
    try:
        report = live_import_preflight(data["job"], auth, settings,
                                       disposable_company_guid=env.get("FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID"))
    except Exception as exc:  # noqa: BLE001 - never a traceback; never "ok"
        return emit(EXIT_INTERNAL, {"ok": False, "reason": "internal_error",
                                    "error": f"unexpected {type(exc).__name__} during the preflight"})
    name = settings.company_name

    def scrub(text: str) -> str:
        return text.replace(name, "<company>") if name else text
    return emit(EXIT_OK if report.ready else EXIT_NOT_READY, {
        "ok": report.ready, "reason": "ok" if report.ready else "not_ready",
        "failed": [c.name for c in report.failed], "request_sha256": report.request_sha256,
        "checks": [{"name": c.name, "ok": c.ok, "detail": scrub(c.detail)} for c in report.checks],
        "summary": scrub(report.summary())})


def _without_company(message: str, env: Mapping[str, str] | None) -> str:
    """``message`` unless it holds the configured company name (client data); then a fixed phrase. Defence in depth:
    the settings' own messages already name the field, not the value."""
    env = os.environ if env is None else env
    name = env.get("FINLEDGER_TALLY_COMPANY")
    if isinstance(name, str) and name.strip() and (name in message or name.strip() in message):
        return "a setting is invalid (details withheld: they would include the company name)"
    return message


def _flag_warnings(env: Mapping[str, str] | None) -> list[str]:
    """A switch set to anything but exactly ``enabled`` stays off (fail closed); say so instead of staying silent."""
    env = os.environ if env is None else env
    return [f"{name} is set but is not exactly 'enabled'; it is OFF" for name in _FLAGS
            if env.get(name) not in (None, "", "enabled")]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
