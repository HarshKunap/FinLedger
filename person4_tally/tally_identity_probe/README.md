# FinLedger identity probe (read-only)

Captures what a real TallyPrime returns for one FinLedger-marked voucher, so the Person 4 identity lookup can be built
on evidence instead of guesses. The requests are **candidates**: until real replies come back, nothing here proves how
TallyPrime answers.

Every request is a single **Export** (read) sent only to `http://127.0.0.1:<port>` on the same PC. No Import, no
`VOUCHER` element, no `ACTION`, no Tally or firewall setting is changed. The one Tally change in the procedure, editing
the voucher amount, is done by a person by hand when the launcher asks.

## One click

1. TallyPrime open with the test company loaded, XML port on 9000.
2. Double-click **`Run-FinLedger-Capture.cmd`** in this folder.
3. Do the one manual step it shows (change only the voucher amount, save, type `DONE`).

Defaults: company `FinLedger Test Company`, document id `9d4f3d5d-499e-41db-b510-e98348e5b503`, voucher date
`20260401`, empty comparison day `20260402`, port `9000`. Override any of them, for example:
`powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-FinLedger-Capture.ps1 -Date 20260402 -OtherDate 20260403`

What it does, all read-only:

| Step | File label | Request |
|---|---|---|
| port check | - | TCP connect to 127.0.0.1:9000 |
| 1 | `01_companies` | Company collection: is the company loaded, exact name |
| 2 | `02_day_before_edit` | Voucher collection, company pinned, voucher day only |
| 3 | `03_fy_before_edit` | Same, whole financial year: finds the voucher by its narration marker without knowing its date or number |
| 4 | `04_other_day` | Same, a day with no voucher: does the date window really filter? |
| you | - | change only the amount in Tally, save, type `DONE` (or `SKIP`) |
| 5 | `05_day_after_edit` | as step 2 |
| 6 | `06_fy_after_edit` | as step 3 |

Output: a new folder `captures\run_<yyyyMMdd_HHmmss>\` next to the launcher holding every `request_<label>.xml`, the
unmodified `response_<label>.xml`, and `summary.txt` (a quick text look: encoding, tag counts, marker hits, GUID /
MASTERID / ALTERID before and after). It also writes `captures\FinLedger_Tally_Capture_run_<...>.zip` with the same
files. Earlier runs are never touched. Don't open and re-save the response files in an editor.

## Company-isolation check (two companies loaded)

Double-click **`Run-Isolation-Check.cmd`** with `FinLedger Test Company` and at least one other existing company
loaded. No Tally step is needed. It:

1. Reads the company list and stops before any voucher request unless exactly one loaded company is named
   `FinLedger Test Company` (exact, case-sensitive), every company has a well-formed GUID, no two share a GUID, and
   at least two (at most 5) companies are loaded.
2. Sends one voucher Export pinned (`SVCURRENTCOMPANY`) to each loaded company. It never pins a request to a company
   that is not loaded: on 2026-10-10 that request crashed TallyPrime 7.1 EDU (Software Exception c0000005, connection
   closed), so how Tally answers a missing company is **unresolved**.
3. Checks every returned voucher GUID starts with the GUID of the company that request was pinned to.

Verdicts in `captures\isolation_<stamp>\isolation_summary.txt` (exit code in brackets): `PASS` (0) every reply held
only its own company and another company's vouchers never leaked; `PARTIAL_PASS` (3) same, but the other companies
had no vouchers, so only one direction was tested; `INCONCLUSIVE` (3) the marker was not found exactly once;
`ISOLATION_NOT_PROVEN` (2) a reply held another company's or malformed vouchers; `STOPPED` (1) the company could not be
determined or Tally did not answer.

Real result, 2026-10-10: **PARTIAL_PASS**. `FinLedger Test Company` pinned returned 1 voucher, its own, with the marker
once; `FinLedger Isolation Test` pinned returned 0 vouchers. Isolation is therefore only partially verified: a second
company holding vouchers has not yet been shown to stay out of the test company's reply.

## Ledger-masters capture (read-only, one double-click)

Done 2026-10-11 (CAPTURED_CLEAN). Double-click **`Run-Masters-Capture.cmd`** (or run
`powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Masters-Capture.ps1`) with TallyPrime open and
`FinLedger Test Company` loaded. It sends three Export requests to 127.0.0.1 only: the company list, the Ledger
collection pinned to that resolved company, and the company list again (same GUID or it stops). It never pins a company
that is not loaded. Nothing in Tally is created, altered, imported or deleted.

Output: `captures\masters_<time>\` (request/response files, `masters_summary.txt`) and
`captures\FinLedger_Masters_masters_<time>.zip`. The replies hold the company's real ledger names; share them only with
the FinLedger team.

| Verdict | Exit | Meaning |
|---|---|---|
| `CAPTURED_CLEAN` | 0 | the ledger reply has the shape FinLedger's parser expects |
| `CAPTURED_WITH_DIFFERENCES` | 3 | captured; the summary lists what differs (that is useful, nothing is wrong with Tally) |
| `STOPPED` | 1 | Tally closed, company not loaded or ambiguous, or the company changed mid-run |

## Open-bills capture (read-only, optional)

`Run-Bills-Capture.cmd` works exactly like the masters capture but sends the Bill collection Export FinLedger's
open-bills read uses (`bill_export_request.xml`). Output: `captures\bills_<time>\` with `bills_summary.txt`, and
`captures\FinLedger_Bills_bills_<time>.zip`. Verdicts and exit codes are the same as the masters capture. The replies
hold real bill references and amounts; share them only with the FinLedger team. Nothing else waits on this run.

## Single capture

`capture.ps1` sends one request; the launcher calls it for each step.
`powershell -NoProfile -ExecutionPolicy Bypass -File .\capture.ps1 -Company "FinLedger Test Company" -Date 20260401 -Label my_label`
(`-ToDate` widens the window; `-Kind company` lists loaded companies; `-Kind ledger` exports ledgers; `-Kind bill` exports bills; `-OutDir` must already exist.)

## Tests

`python -m pytest person4_tally/tests -q`. The static checks always run; the checks that run the scripts against a
fake local Tally need PowerShell (`pwsh` on PATH, or `FINLEDGER_PWSH=/path/to/pwsh`) and are skipped without it.
The fake replies are synthetic and say nothing about real TallyPrime output.
