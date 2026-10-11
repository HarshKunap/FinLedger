# Person 4: completion matrix (single source of truth, 2026-10-11)

Every capability has exactly one status. When a capability spans several levels, the status is the **lowest level
that limits release use**: for example, unit-tested code that has never met real Tally on its critical path is `F`, not
`A`. The README and the completion report link here and do not keep their own matrix.

| Code | Meaning |
|---|---|
| **A** | unit-tested (synthetic Tally, no platform rows) |
| **B** | integration-tested (real P2 migrations and the real P3 `ControlService` on Postgres; Tally still synthetic) |
| **C** | verified against real TallyPrime (EDU 7.1, Harshith's PC, read-only captures) |
| **D** | partially verified (part real or integrated, part only synthetic) |
| **E** | blocked by a missing shared contract (Person 2 / Person 3 / `packages/contracts`) |
| **F** | blocked by missing external evidence (a real Tally reply nobody has captured yet) |
| **G** | not implemented |

| # | Capability | Status | Evidence | What moves it up |
|---|---|---|---|---|
| 1 | Shared contracts (P4's use of them) | **E** | `test_contract_compat.py` pins 16 upstream facts; gaps 1–8, 10, 11, 13 open | P2/P3 changes in `CONTRACT_GAPS.md` |
| 2 | Approval boundary | **B** | `test_release_boundary.py` (missing, rejected, stale, superseded, wrong action, payment kind, other document, cross-client, cross-company, duplicate, posted, uncertain, persistence, restart); real P3 rows in `test_p3_integration.py` (approve, edit after approval, erp_id) | — (complete for what the contract carries) |
| 3 | Company identity (name + GUID) | **C** | real company list captures; `test_identity.py`, `test_reads.py` | — |
| 4 | Company isolation | **D** | real two-company run (second company had no vouchers); GUID-prefix checks unit-tested | optional isolation run with two populated companies |
| 5 | Ledger masters | **C** | real capture `masters_20261011_013427`, byte-identical request; `test_real_masters.py` | — |
| 6 | Ledger mapping | **A** | `test_masters.py`, aliases refused; P2 `mapping_table` rows not read from a database yet | P2 agent route for mapping reads |
| 7 | Purchase preview | **D** | golden XML built from the **real** ledger masters with no socket; Tally has never imported it | the authorized live test (`LIVE_IMPORT_TEST_PROCEDURE.md`) |
| 8 | Credit / debit note preview | **E** | refused with `unsupported_voucher_type`, no network call | gap 8 (+ policy decision) |
| 9 | Posting state machine | **B** | `test_posting.py`, `test_hardening.py`, `test_release_boundary.py`, `test_end_to_end.py`, real P3 rows end to end | live test for the post-send half |
| 10 | Durable `send_started` | **A** | fsync'd journal; failure before it = nothing sent; failure after = uncertain (every boundary fault-injected) | — (local disk semantics; no platform mirror until gap 1/2) |
| 11 | Import transport | **F** | loopback-only, one attempt, timeouts; synthetic Tally only | live test |
| 12 | Import response validation | **F** | strict `parse_import_reply` + post-send lookup; reply shape assumed, never seen | live test (captures the real reply) |
| 13 | Voucher lookup | **C** | real voucher export, GUID prefix, MASTERID/ALTERID, marker survives an edit, date filter ignored | — |
| 14 | Reconciliation | **D** | the lookup it uses is real-verified; matching after a real Import is not | live test |
| 15 | Open bills | **F** | strict parser, payables rule, `OpenBillsIn` payload; no real Bill reply captured | `Run-Bills-Capture.cmd` (read-only) |
| 16 | Offline XML | **A** | block recorded before the file; write failures stay blocked | — |
| 17 | Exception handling | **A** | no exception becomes success; persistence boundaries; agent survives source/sink/connector errors | — |
| 18 | Agent lifecycle | **A** | lock, health re-check, status, signal stop, shutdown in seven phases, restart | Windows wrapper (row 22) |
| 19 | Job pickup and result reporting | **E** | `JobSource`/`ResultSink` interfaces; no P2 agent route (gap 10) | gap 10 |
| 20 | Configuration | **A** | strict parsing, posting off unless exactly `enabled`, redacted output | — |
| 21 | Operator CLI | **A** | `config`/`health`, exit codes 0/1/2/3/64, no post command or bypass flag | run `health` on the Tally PC |
| 22 | Windows deployment | **G** | assessed and documented (`WINDOWS_DEPLOYMENT.md`); nothing built or installed | gaps 10 and 20 decided first |
| 23 | Security | **A** | guards and refusals unit-tested (incl. DTD refusal); audit in the completion report | — |
| 24 | Observability | **A** | `AgentHost.status()`; logs carry ids and exception types only, bounded (tested) | log handler set by the wrapper |
| 25 | End-to-end tests | **B** | steps 1–16 synthetic (`test_end_to_end.py`); real P3 rows through the agent (`test_end_to_end_with_real_p3_rows`) | live test for steps 9–12 on real Tally |
| 26 | Release documentation | **D** | README, gaps, matrix, Windows assessment, live-test procedure; the procedure is unexecuted | executing the procedure once, when authorized |

## Supported voucher types and preview

| Voucher | Preview | Post | Why |
|---|---|---|---|
| Purchase, GST `exempt`, no TDS, bill-wise `new_ref` | yes | yes (gated; never live-run) | the only shape the shared draft describes completely |
| Purchase with GST lines | refused `unsupported_tax` | no | gap 6 (untyped tax lines) + policy |
| Purchase with TDS | refused `unsupported_tax` | no | gap 7 + policy |
| Bill-wise `against_ref` / `advance` / `on_account` | refused `unsupported_bill_wise` | no | allocation semantics not in the contract |
| Debit note / credit note | refused `unsupported_voucher_type` | no | gap 8 |
| Payment | refused `unsupported_voucher_type` | no | out of scope until purchase posting is live-verified |
| P3 `journal` | refused (`unsupported_voucher_type`) | no | gap 13 (not in the shared enum) |
