# Person 4 — Tally Connector

> Status (2026-10-11, release-readiness pass): the read-only client, purchase preview, gated posting state machine,
> reconciliation, offline XML, open bills, health, connector interface, agent host and a read-only operator CLI are
> implemented and tested (658 tests, including steps 1–16 end to end and the real P3 rows). **Live posting is
> off by default and has never been run against a real Tally.** Real TallyPrime (EDU 7.1) replies have been seen for
> the company list, voucher export and **ledger export**; the Import and Bill replies have not. See
> [Real vs simulated](#real-evidence-vs-simulated) before relying on anything here.

Original plan: [ARCHITECTURE.md](ARCHITECTURE.md). Release documents:

| Document | What it holds |
|---|---|
| [docs/COMPLETION_MATRIX.md](docs/COMPLETION_MATRIX.md) | **the** status of every capability (A–G) and the supported voucher types |
| [docs/CONTRACT_GAPS.md](docs/CONTRACT_GAPS.md) | the 20 contract gaps one by one, with proposals for Persons 2/3 and upstream changes already present |
| [docs/WINDOWS_DEPLOYMENT.md](docs/WINDOWS_DEPLOYMENT.md) | deployment assessment (nothing is installed) |
| [docs/LIVE_IMPORT_TEST_PROCEDURE.md](docs/LIVE_IMPORT_TEST_PROCEDURE.md) | the controlled first live Import, **not executed**, needs explicit authorization |

## Architecture

```
P3 approved post job ──► authorization ──► journal gate ──► company + masters ──► preview ──► pre-send lookup
                         (P3 rows, hash,    (durable,         (GUID-pinned,       (pure XML)    (marker already
                          erp_id)            local)            real-verified)                    in Tally?)
                                                                                                    │
     erp_result dict ◄── POSTED / TALLY_REJECTED / OUTCOME_UNCERTAIN ◄── post-send lookup ◄── one Import, no retry
```

| Module | Role | Network |
|---|---|---|
| `transport.py` | The only socket. `127.0.0.1` only (no host setting), timeouts, bounded replies, export/import guards, no retries | yes |
| `company.py` | Company list, exact-name resolve, paired-GUID check, `pinned_export` (resolve → export → re-resolve) | via transport |
| `identity.py` | Parsers: company list, voucher export, narration marker, `find_by_marker` | none |
| `masters.py` | Ledger collection export and strict parser (`LedgerMasters`, with aliases) | via company |
| `mapping.py` | P2 `mapping_table` rows → exact ledger resolution; never creates; aliases never matched | none |
| `voucher_xml.py` | `preview_purchase_xml`: deterministic purchase Import XML, or a list of blocking problems | none |
| `lookup.py` | Read-only voucher lookup by marker; any failure is `unreadable`, never `not_found` | via company |
| `journal.py` | Append-only, fsync'd per-document journal and the resend `gate` | disk only |
| `posting.py` | `post_approved`, `reconcile`, `clear_for_resend`, offline XML, `PostOutcome.erp_result()` | via transport |
| `bills.py` | Read-only open bills → P2 `OpenBillsIn` payload (payables only) | via company |
| `connector.py` | `TallyConnector`: the shared adapter methods (below) | via above |
| `agent.py` | `AgentHost`: startup health, single-instance lock, one attempt per job, health re-check, status, signals | via connector |
| `__main__.py` | `python -m finledger_tally config|health|sync|preflight`: read-only operator commands with exit codes | health only |
| `config.py` | `AgentSettings.from_env`, `redacted()` | none |

### Shared adapter methods (ARCHITECTURE.md "Shared connector contract") — method audit

| Method | Returns / raises | Network | Can Import | Real-Tally evidence | Tests |
|---|---|---|---|---|---|
| `connect()` | `TallyCompany`; raises `CompanyNotResolved` / `TallyUnavailable` | Export only | no | company list: **real** | test_reads, test_transport |
| `health(check_masters=True)` | `HealthReport` (never raises for Tally faults; status says why) | Export only | no | company + ledger: **real** | test_reads, test_agent |
| `fetch_masters()` | `LedgerMasters`; raises on any doubt | Export only | no | **real** ledger reply | test_masters, test_real_masters |
| `masters_snapshot()` | P2 `POST /agent/masters` bodies (validated against `MastersIn`) | Export only | no | **real** | test_reads |
| `fetch_open_bills()` | `OpenBills` (+ `classify()`, `platform_payload()` for `OpenBillsIn`); raises on failure | Export only | no | none (synthetic) | test_bills, test_reads |
| `post_invoice(job, auth, mapping=, expected_total=)` | `PostOutcome`; `.erp_result()` → P3 kwargs or None | Export + one Import | **only path** | none (never run live) | test_posting, test_hardening, test_p3_integration |
| `post_credit_debit(job, auth)` | `PostOutcome(validation_rejected, unsupported_voucher_type)` | none | no | — | test_posting |
| `post_payment(job, auth)` | `PostOutcome(validation_rejected, unsupported_voucher_type)` | none | no | — | test_posting |

Pure helpers (no network): `preview_purchase_xml`, `parse_*`, `resolve_ledger/resolve_party`, `find_by_marker`,
`gate`, `PostAuthorization.from_rows`. Read-only network helpers: `lookup_document`, `reconcile`.

## Running the tests

From the repo root (no test talks to a real Tally; every Tally is an in-process fake on an ephemeral 127.0.0.1 port):

```bash
# 1. Without PowerShell or Postgres (kit-script and P3 hand-off tests skip):
python -B -m pytest person4_tally/tests -q -p no:cacheprovider                      # 597 passed, 56 skipped

# 2. With PowerShell 7 for the capture-kit tests:
FINLEDGER_PWSH=/path/to/pwsh python -B -m pytest person4_tally/tests -q -p no:cacheprovider   # 651 passed, 2 skipped

# 3. Everything, including the real P3 -> P4 hand-off on Postgres with P2's migrations
#    (a Python with psycopg, psycopg_pool, fastapi; an owner DSN of a SCRATCH cluster):
FINLEDGER_PWSH=/path/to/pwsh FINLEDGER_TEST_OWNER_DSN=postgresql://postgres@127.0.0.1:55439/postgres \
PYTHONPATH=person2_platform/src  $VENV/bin/python -B -m pytest person4_tally/tests -q -p no:cacheprovider  # 658 passed
```

As root, `initdb` refuses to run; start a throwaway cluster as the `postgres` user and pass its DSN.

Release-readiness files: `test_release_boundary.py` (approval boundary, every persistence boundary, CLI, agent
shutdown in seven phases, bounded logs), `test_end_to_end.py` (steps 1–16), `test_contract_compat.py` (upstream
contract pins; a failure there means Person 2/3 changed a contract, see CONTRACT_GAPS.md).

## Configuration (`AgentSettings.from_env`)

| Variable | Meaning | Default |
|---|---|---|
| `FINLEDGER_TALLY_PORT` | Tally's XML port on **127.0.0.1** (there is deliberately no host setting) | 9000 |
| `FINLEDGER_TALLY_TIMEOUT` | seconds per request | 30 |
| `FINLEDGER_TALLY_MAX_REPLY_BYTES` | reply size bound | 64 MiB |
| `FINLEDGER_TALLY_CLIENT_ID` | FinLedger client this agent is paired with | none (posting refused) |
| `FINLEDGER_TALLY_COMPANY` | exact Tally company name (`erp_links.company_name`) | none |
| `FINLEDGER_TALLY_COMPANY_GUID` | that company's GUID, read from `health()` at pairing | none (posting refused) |
| `FINLEDGER_TALLY_STATE_DIR` | journal, raw request/response bytes, offline XML, masters cache | `./finledger_tally_state` |
| `FINLEDGER_TALLY_POSTING` | live posting only if exactly `enabled` | off |
| `FINLEDGER_TALLY_OFFLINE_XML` | write manual-import XML when Tally is down, only if exactly `enabled` | off |

Never expose Tally's port beyond the PC (no port forwarding, no `0.0.0.0` listener). `settings.redacted()` is what may be
logged. Logs carry ids, digests, states and exception types only, never invoice contents.

## Company identity

Every company-scoped read goes through `company.pinned_export`: list companies → one **exact, case-sensitive** name
match → (when paired) GUID must equal `FINLEDGER_TALLY_COMPANY_GUID` → send the pinned export → list again, same name and
GUID. A name alone is never trusted. A company that is not loaded is never pinned (that crashed the real Tally). Every
returned voucher and ledger must have a GUID of the form `<company GUID>-<8 hex>` of the resolved company, or the whole
reply is refused.

**Isolation is partially verified only.** Real evidence: with two companies loaded, each pinned voucher export returned
only its own vouchers, but the second company held none. Missing for full isolation: a pinned export of company A while
company B also holds vouchers (and ledgers), showing no B rows. This is optional manual validation; the GUID-prefix check
above already refuses foreign rows regardless.

## Masters (ledger parsing) — checked against real Tally

`fetch_ledger_masters` reads the Ledger collection and refuses the whole reply on: empty replies, a NAME attribute and
direct NAME child that disagree, a name missing from its own `LANGUAGENAME.LIST/NAME.LIST`, missing or blank PARENT,
GUIDs of another company or without the `-<8 hex>` suffix, duplicate GUIDs, names duplicated ignoring case, aliases that
collide with another ledger or appear twice, `STATUS` other than 1, malformed XML.

Real reply (`tests/fixtures/tallyprime_7_1_edu_masters_20261011_013427`, checked by `tests/test_real_masters.py`):
UTF-8 without BOM, CRLF; `DESC/CMPINFO` before `DATA` (its counters are never used: they were 0 here and non-zero in an
earlier company capture); one `COLLECTION`; `LEDGER` rows whose name is the `NAME` attribute with **no direct `NAME`
child**, repeated in `LANGUAGENAME.LIST/NAME.LIST/NAME`; `PARENT` and `GUID` with `TYPE="String"`; ledger GUIDs
`<company GUID>-<8 hex>`; reserved group written `&#4; Primary` (read as `Primary`). The Python request bytes equal the
real captured request. Seen on one small company (4 ledgers, no aliases), so alias handling rests on Tally's documented
model and fails closed.

## Mapping

`resolve_ledger` / `resolve_party` apply P2 `mapping_table` rows (`party`/`ledger` kinds) and report `missing_ledger`,
`ambiguous_ledger` (case-only matches, alias matches, a GSTIN mapping that disagrees with the draft),
`masters_unavailable` or `company_mismatch`. Nothing is ever created: `party_create_proposal=True` blocks.

## Purchase preview

`preview_purchase_xml(job, company=, expected_company_name=, masters=, expected_total=, mapping=)` is pure (no network).
It returns either `ok=True` with deterministic XML bytes, their sha256 and metadata (`voucher_type`, `voucher_date`,
`reference`, `lines`, `stage="preview_ready"`), or a list of problems. Blocked on purpose: GST tax lines, TDS,
debit/credit notes, payments, reverse charge, journal/non-GST, amounts above `numeric(14,2)`, NaN/infinite/bool amounts,
more than 2 decimals (never rounded), invented due dates. A preview is never a posted voucher. Goldens:
`tests/fixtures/preview/`.

## Posting control

`post_approved(job, auth, settings)` does nothing unless **all** hold:

1. `FINLEDGER_TALLY_POSTING=enabled`, client id and company GUID configured;
2. `PostAuthorization` (use `PostAuthorization.from_rows(client_id, documents_row, posting_attempts_row,
   approvals_rows)`) built from **P2/P3's own rows**. `from_rows` needs the id, client and `superseded_at` columns and
   raises `ValueError` if the rows belong to more than one document or client. Posting then needs: a
   **non-superseded** `approvals` row of kind `human` or `policy_clear` for the job's revision (`payment_release`
   does not count; no rows = not approved; an approval superseded by an edit = `approval_superseded`), rows for the
   job's own document (`document_mismatch`), document `approved`/`scheduled`, attempt `pending`, same client,
   revision, idempotency key, and `request_hash == sha256(canonical job)` (P3's formula), so an edited job is refused;
3. the local journal gate is `clear`;
4. the company resolves to the paired GUID, masters load, the preview has no problems;
5. a pre-send lookup does **not** find the document's marker already in Tally.

If P3's attempt is already `posted` with an `erp_id` for this exact job, that id is returned (`already_posted`) and
Tally is not contacted (ARCHITECTURE.md idempotency rule).

Then the exact request bytes are saved, `send_started` is fsync'd, and **one** Import is sent. No retry exists anywhere.
`POSTED` requires Tally's reply to say created **and** a fresh lookup to find exactly one voucher with the marker, the
same reference and the same party amount. HTTP 200 alone is never success.

### Journal phases → P2/P3 states

P2's `posting_attempts.result` only has pending/posted/failed (gap 1), so P4 keeps its finer states in its own journal:

| Journal phase | Meaning | Reported to P3 as |
|---|---|---|
| `validation_rejected` | draft can't be posted (missing ledger, GST, …) | `failed` → exception |
| `preview_ready` | XML built and checked | — |
| `send_started` | about to send; blocks every resend until settled | — |
| `send_not_delivered` | connection refused before any byte left | not reported (stays approved) |
| `response_received` | reply bytes saved | — |
| `posted` / `reconciled` | confirmed in Tally by lookup | `posted` with `erp_id` (voucher GUID) |
| `tally_rejected` | reply rejected and lookup finds nothing | `failed` → exception |
| `outcome_uncertain` / `reconciliation_required` | can't tell | `failed` with "outcome_uncertain … will not resend" |
| `offline_artifact_issued` | manual-import XML handed out | not reported; live posting blocked |
| `human_cleared` | a named person unblocked it | — |

## Uncertain outcomes and failures

| What happened | State | Resend? |
|---|---|---|
| connection refused before anything was sent | `send_not_delivered` | allowed (gate clears) |
| sent, then timeout / hang-up / oversized reply | `outcome_uncertain` + one read-only lookup | **blocked** until reconciled or cleared by a person |
| reply says created but lookup can't confirm | `outcome_uncertain` | blocked |
| reply says rejected and lookup finds nothing | `tally_rejected` | allowed after a new approval |
| marker already in Tally before sending | `blocked` (`marker_already_in_tally`) | blocked; reconcile |
| journal can't record `send_started` | `blocked` (`journal_write_failed`) | nothing sent |
| journal/response file can't be written after sending | `outcome_uncertain` | blocked (`send_started` is durable) |
| offline file can't be written | `blocked` (`artifact_write_failed`) | blocked (recorded first) |

A timeout is never read as "not created", `not_found` is never proof of absence, and nothing claims exactly-once.

## Voucher identity and reconciliation

Real evidence (2026-10-10 capture): the narration marker `finledger:document_id=<uuid>` survived a manual amount edit;
voucher GUID and MASTERID stayed the same, ALTERID went 3 → 4; GUID = `<company GUID>-<8 hex>`; the date window was
ignored. Still assumed: that the marker survives an Import (no import has been sent), and what the GUID suffix means
(never interpreted). The lookup refuses malformed GUIDs, foreign-company rows, duplicate GUIDs, duplicate MASTERIDs and
duplicate markers, and returns `found` / `ambiguous` / `unreadable` / `not_found`.

`reconcile(document_id, around, settings)` is read-only: a single matching voucher moves the journal to `reconciled`.
Not found or ambiguous leaves it blocked. `clear_for_resend(document_id, settings, actor=, note=)` is the only way to
unblock, needs a named person and a note of 10+ characters, and refuses once posted. Human review is needed whenever
the outcome is uncertain, the marker appears more than once, or a voucher with the marker differs in reference/amount.

## Offline XML

With `FINLEDGER_TALLY_OFFLINE_XML=enabled` and Tally unreachable, a validated preview (using the masters cached at the
last successful read) is written as `offline_xml/<doc>/r<rev>_<sha12>.xml` plus a `.json` of ids and digests marked
"NOT IMPORTED". The journal records `offline_artifact_issued` **before** the file is written, so a file can never exist
while live posting is unblocked.

## Attachments

The source PDF is **not** attached in Tally. The narration marker is how FinLedger finds the voucher again (gap 12).

## Open bills

`fetch_open_bills` → `OpenBills.platform_payload()` matches P2 `OpenBillsIn` (`due_date` and `vendor_gstin` always
`None`). Every bill's party must be an exact ledger of the same company's masters, read in the same call, and the
company must be the same before and after both reads. Amounts are exact Decimals (≤ 12 digits, ≤ 2 decimals, no
signs other than `-`, never rounded). Empty is a valid result; a failed or rejected read raises.

Payables rule (interim, until a real Bill reply and an accounting decision): a bill is sent only when its party's
parent group is exactly `Sundry Creditors` and its closing balance is positive. Everything else is kept in
`OpenBills.classify()[1]` with a reason (debtors, sub-groups, zero or debit balances), never dropped silently.
**The Bill reply shape is unverified**; `Run-Bills-Capture.cmd` has not been run yet.

## Agent host and Windows status

`AgentHost(settings, source, sink)`:
- `run()` takes an OS lock on the state folder (`AgentAlreadyRunning` if another agent holds it; the OS frees it if
  the process dies), runs `health()`, and claims nothing unless the company is verified and posting is enabled. A
  health check that raises means "not ready".
- each tick claims one job, calls `post_invoice` once, reports `erp_result` when there is something to report. No
  retries. After a tick that found Tally unavailable or the company unverified, health is re-checked before the next
  claim, every tick, until it passes.
- `stop` (or `stop_on_signals(host)` for Ctrl+C / SIGTERM / SIGBREAK) ends the loop after the current job.
- `status()` gives a JSON-safe dict (ready, tally status, counts by state, last error, redacted settings; no company
  name, no invoice data).
- restart safety comes from the journal, not the process: tests start a second host on the same state after an
  uncertain send (blocked, no Import) and after a post (`already_posted`, no Import).

`JobSource`/`ResultSink` are interfaces because P2 has no agent route to claim post jobs or report results (gap 10),
so there is deliberately no `run` command and no invented queue.

A failing `JobSource.claim()` (platform unreachable) or a claim that is not a `ClaimedJob` is recorded as an error
for that tick; the loop continues and nothing is posted. Error text in `TickResult.error` is bounded to 300
characters, and `status()["last_error"]` holds only a label such as `claim failed: ConnectionError`.

Operator CLI (read-only): `python -m finledger_tally config` prints redacted settings (exit 1 if invalid);
`python -m finledger_tally health` prints one health check: exit 0 only when the company is verified **and** its
ledger masters were read, 2 not ready, 3 unexpected internal error (only the exception type is printed), 64 usage.
`python -m finledger_tally sync` reads ledger masters and open bills from the verified company (read-only) and sends
them to person2's existing `POST /agent/masters` and `POST /agent/open-bills` with `FINLEDGER_PLATFORM_URL` (https)
and `FINLEDGER_AGENT_TOKEN` (from `/clients/{id}/erp/pair`); exit 0 only if every snapshot was accepted. The token
is never printed and redirects are refused. `python -m finledger_tally preflight FILE.json` runs the read-only
live-Import checks (docs/LIVE_IMPORT_TEST_PROCEDURE.md).
There is no post or run command and no flag that bypasses approval.

**Windows service: not built, not installed.** The full assessment (prerequisites, configuration, folder ACLs,
logging, startup/shutdown, upgrade, reboot and Tally-down recovery, uninstall, health checks) is in
[docs/WINDOWS_DEPLOYMENT.md](docs/WINDOWS_DEPLOYMENT.md). A logon task is preferred over a session-0 service, because
Tally must run in the user's session for its XML port to answer. Always set `FINLEDGER_TALLY_STATE_DIR` to an
absolute path.

## Read-only diagnostics (PC kit, `tally_identity_probe/`)

| Double-click | What it reads |
|---|---|
| `Run-FinLedger-Capture.cmd` | company list + voucher exports (done 2026-10-10) |
| `Run-Isolation-Check.cmd` | each loaded company's pinned voucher export (PARTIAL_PASS) |
| `Run-Masters-Capture.cmd` | company list + ledger export + company list (CAPTURED_CLEAN 2026-10-11) |
| `Run-Bills-Capture.cmd` | company list + Bill export + company list (**optional**, not yet run) |

All send Export requests to 127.0.0.1 only and never pin a company that is not loaded.

## Security boundaries

- Only `transport.py` opens sockets, only to 127.0.0.1; drafts never carry an endpoint.
- Read functions go through `export()`, which refuses anything with Import, TALLYMESSAGE, VOUCHER or ACTION.
- The only Import path is `post_approved`, and the Import guard accepts exactly one `VOUCHER ACTION="Create"`.
- XML is built from fixed templates with every value escaped; no LLM or caller XML is accepted.
- Replies that declare a DTD or entities are refused before parsing (real Tally replies carry none).
- Journal paths come only from canonical UUIDs; journal files are created 0600, folders 0700 (on Windows the
  folder ACL must be set by the installer).
- Logs carry ids, states and exception type names only, bounded per line (tested: no ledger names, references,
  amounts, narration or company names).
- Residual risks: the journal is local (losing the state dir loses the resend guard; the pre-send lookup still runs);
  the paired GUID is typed in by a person; the Import reply parser and Bill parser are unverified against real Tally.

## Real evidence vs simulated

| Area | Status |
|---|---|
| Company list reply, company GUID, re-check before/after | **verified on real Tally** |
| Voucher export shape, voucher GUID prefix, MASTERID stable, ALTERID changes on edit | **verified on real Tally** |
| Date filter ignored; pin to an unloaded company crashes Tally | **verified on real Tally** |
| Ledger export request and reply shape, ledger GUID prefix, `&#4; Primary` | **verified on real Tally** (one company, 4 ledgers) |
| Ledger aliases | simulated (none in the real reply) |
| Multi-company isolation | **partially verified** |
| Import (create) reply and counters, marker surviving an Import | simulated |
| Bill reply shape and sign convention | simulated |

## Manual validation still needed

1. Optional: `Run-Bills-Capture.cmd` on the Tally PC (read-only), to verify the open-bills parser.
2. A first live post, **only after explicit authorization**, on a disposable test company, following
   [docs/LIVE_IMPORT_TEST_PROCEDURE.md](docs/LIVE_IMPORT_TEST_PROCEDURE.md).
3. Optional: an isolation run with a second company that holds vouchers.

## Troubleshooting

| Symptom | Meaning |
|---|---|
| `health().status == "tally_unreachable"` | Tally not running, or the XML port is not the configured one |
| `company_unresolved` | the configured name is not loaded; names are case-sensitive |
| `company_ambiguous` | two loaded companies share the name (or differ only in case); load only the paired one |
| `company_guid_mismatch` | a different company with the same name is loaded; posting refused |
| `posting == "enabled_but_not_ready"` | posting enabled but client id / GUID / company is missing |
| `blocked: reconciliation_required` | an earlier send is uncertain; run `reconcile`, then (if truly absent) `clear_for_resend` |
| `blocked: journal_write_failed` | the state dir can't be written; nothing was sent |
| `JournalCorrupt` | a journal line is torn or edited; posting stops for that document until a person inspects it |
| CLI exit 3 | an unexpected internal error in `health`; only its type is printed; report it |
| `not_authorized: approval_superseded` | P3 recorded an edit after approval; the job is stale, P3 must approve the new revision |
| `ValueError: … not for the same document/client` | the caller combined rows of different documents or clients |

## Completion matrix

The single source of truth is [docs/COMPLETION_MATRIX.md](docs/COMPLETION_MATRIX.md) (statuses A–G with evidence).
