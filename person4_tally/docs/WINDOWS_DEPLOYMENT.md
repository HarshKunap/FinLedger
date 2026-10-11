# Person 4: Windows deployment assessment (2026-10-11)

> **Status: assessment only.** Nothing is installed on any PC, no service or scheduled task exists, and the agent
> cannot run unattended yet: Person 2 has no route to hand it post jobs (contract gap 10). This page says what a
> deployment needs and how the existing code behaves, so the wrapper can be built quickly once the routes exist.

What runs today on the Tally PC: the read-only capture kit (`tally_identity_probe/*.cmd`) and, if Python is present,
`python -m finledger_tally config|health` (read-only). Nothing on this page has been executed on Harshith's PC.

## 1. Installation prerequisites

| Item | Requirement | Why |
|---|---|---|
| Windows | 10/11 or Server 2019+, the PC where TallyPrime runs | Tally's XML port listens on that machine; the agent only ever connects to `127.0.0.1` |
| TallyPrime | XML server enabled (Help > Settings > Connectivity > Client/Server configuration: "TallyPrime acts as" Server, port 9000 by default) | the only interface used |
| Python | 3.11 or newer, per-user or machine install (tested on 3.13) | the agent is a Python package |
| Account | a normal (non-admin) Windows user, the same one that runs Tally | Tally answers only while it runs in that user's session |
| Network | outbound HTTPS to the FinLedger platform only (once gap 10 exists); **no inbound rule** | the agent polls; Tally's port must never be exposed (`127.0.0.1` is hard-coded) |

## 2. Runtime dependencies

- Python standard library (http.client, xml.etree, fcntl/msvcrt, signal, logging).
- `pydantic` 2.x, for the shared `contracts.schemas.VoucherDraft` (from `packages/contracts`).
- The two source trees on `PYTHONPATH`: `person4_tally/src` and `packages`. There is no wheel or installer yet; a
  release would vendor both into one folder.
- No database, no browser, no admin rights, no Windows service framework.

## 3. Configuration location

Environment variables only (`AgentSettings.from_env`; see the README table). Recommended: a wrapper `.cmd` that sets
them, kept in the agent folder and readable only by the agent's user:

```
set FINLEDGER_TALLY_COMPANY=<exact company name>
set FINLEDGER_TALLY_COMPANY_GUID=<GUID shown by: python -m finledger_tally health>
set FINLEDGER_TALLY_CLIENT_ID=<client uuid from pairing>
set FINLEDGER_TALLY_STATE_DIR=%LOCALAPPDATA%\FinLedger\state
rem FINLEDGER_TALLY_POSTING stays unset (off) until a person deliberately enables it
```

**Always set `FINLEDGER_TALLY_STATE_DIR` to an absolute path.** The default (`finledger_tally_state`) is relative to
the working directory, and a wrapper started from another folder would get an empty journal, which loses the resend
guard (the pre-send marker lookup still runs). The pairing token (once gap 10/20 exist) belongs in Windows Credential
Manager or a DPAPI-protected file, never in this `.cmd`.

## 4. Data-folder permissions

`%LOCALAPPDATA%\FinLedger\state` holds `journal\<document-uuid>\` (journal, exact request/response bytes),
`offline\` (manual-import XML), `masters_cache.json`, and `agent.lock`. The code creates folders `0700` and files
`0600`, which on Windows map only to the read-only attribute, so **the ACL must be set by the installer**: inherit
off; full control for the agent's user and SYSTEM; nobody else. The journal holds invoice references and amounts.
Keep it on a local disk (not OneDrive or a network share): the journal relies on `fsync` and on an OS file lock.

## 5. Logging location

The package logs through Python `logging` (`finledger_tally.*`) and configures no handler. The wrapper must add a
`RotatingFileHandler` at `%LOCALAPPDATA%\FinLedger\logs\agent.log` (for example 5 × 1 MB). Log lines carry ids,
states and exception type names only (tested: no ledger names, references, amounts, narration or company names),
and each line is bounded.

## 6. Startup and shutdown behaviour (existing code, tested)

- Startup: `AgentHost.run` takes an exclusive OS lock on `state\agent.lock` (`AgentAlreadyRunning` → the wrapper exits
  non-zero), runs `health()`, and claims nothing unless the company and GUID verify and posting is enabled.
- While Tally or the company is missing: no claims; health is re-checked every tick until it passes.
- Shutdown: `stop_on_signals(host)` maps Ctrl+C, SIGTERM and SIGBREAK (Ctrl+Break) to `stop`; the
  current job finishes and is journaled, nothing new is claimed, the lock is released. Tested in seven phases (idle,
  failing health check, validation, pre-send persistence, uncertain submission, post-send persistence,
  reconciliation).
- A hard kill (power loss) at any point leaves either nothing sent, or a durable `send_started` that blocks the
  document until it is reconciled.

**Recommended wrapper:** a Task Scheduler task "At log on of <user>", "Run only when user is logged on", restart on
failure every 5 minutes, that runs the `.cmd` above and then the (future) `run` entry point. A session-0 Windows
service is not recommended: Tally runs in the user's session, and the spec asks for a site agent, not a service.

## 7. Upgrade procedure

1. Stop the task (or press Ctrl+C in its console); confirm `agent.lock` is released (a new `health` run works).
2. Check no document is mid-send: every journal must end in a settled phase, or be known-blocked for reconciliation.
3. Replace the program folder (never the state folder). Keep the previous folder for rollback.
4. Run `python -m finledger_tally config` and `health` (both read-only); expect exit 0.
5. Start the task. The journal format is append-only and versioned by phase names; a new version must read old
   journals (add a test with a journal from the previous release before shipping).

## 8. Recovery after reboot

The logon task starts the agent after the user logs in. Until Tally is open with the company loaded, health reports
`tally_unreachable` or `company_unresolved` and nothing is claimed. Any document whose send started before the reboot
stays blocked (`reconciliation_required`) until `reconcile` finds its voucher or a person clears it; it is never
re-sent automatically.

## 9. Recovery after Tally becomes unavailable

Before a send: `tally_unavailable`, nothing sent, nothing reported, the job stays with the platform; health is
re-checked each tick. During a send: a refused connection is `send_not_delivered` (safe to try again later); anything
after bytes may have left (timeout, reset, partial reply) is `outcome_uncertain`, blocked, reported to P3 as
"reconciliation required; FinLedger will not resend". With `FINLEDGER_TALLY_OFFLINE_XML=enabled`, a down Tally
produces a manual-import file instead, and live posting for that document stays blocked until a person clears it.

## 10. Uninstallation

1. Disable and delete the scheduled task.
2. Check the journal for blocked documents and settle them first (they are the only record of an uncertain send).
3. Archive the state folder (journal + request/response bytes) to the firm's records, then delete the program
   folder. Delete the state folder only after the archive is confirmed. Remove the stored pairing token.
4. Invalidate the agent token on the platform. P2 has no revoke route yet (`rotate-token` rotates the
   upload link, not the agent token); re-pairing via `POST /clients/{id}/erp/pair` replaces the token hash.

## 11. Operational health checks

| Check | Command / source | Healthy |
|---|---|---|
| Configuration | `python -m finledger_tally config` | exit 0; `company_name_set`/`company_guid_set` true |
| Tally + company + masters | `python -m finledger_tally health` | exit 0, `company_verified`, `masters: ok` |
| Not ready | same | exit 2 with the reason (`tally_unreachable`, `company_guid_mismatch`, …) |
| Masters and open bills in person2 | `python -m finledger_tally sync` as a scheduled task (e.g. hourly) | exit 0, every result `sent: true` |
| Agent internals | `AgentHost.status()` (exposed by the wrapper) | `ready_to_post`, counts by state, `last_error` (type only) |
| Blocked documents | P3 exception tray (`outcome_uncertain …`, `blocked: …`) | empty, or each one being reconciled |
| Disk | free space on the state drive | a full disk blocks sends (`journal_write_failed`, nothing sent) |
