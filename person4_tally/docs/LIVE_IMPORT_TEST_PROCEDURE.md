# Person 4: controlled live Import test (procedure only, NOT executed)

> **This procedure has not been run, and nothing in this repository runs it.** The checks are code
> (`finledger_tally.preflight`) and the read-only part is a CLI command (`python -m finledger_tally preflight`), but
> there is **no** CLI command, script or flag that sends the Import. The one send is a deliberate Python call
> (`execute_live_import`) that refuses unless every check passes again and the person types the exact confirmation
> phrase. Running it needs Harshith's explicit, separate authorization at the time, a person at the Tally PC, and the
> stop rules below. Until then the Import reply (contract gap 17) stays unverified.

## 1. Why one live Import is needed

Everything before the Import is verified on real TallyPrime 7.1 (company list, ledger masters, voucher export, marker
lookup). The Import itself is not:

- the **reply shape** is assumed from Tally's documentation (`parse_import_reply`); if real Tally answers differently,
  every live post would end as `outcome_uncertain` (safe, but useless), or a misread could be wrong;
- Tally has **never accepted the preview XML** (ledger names, sign convention, bill-wise block, date format);
- the **narration marker after an Import** (not just after a manual edit) has not been seen in a voucher export;
- the post-send lookup has never matched a voucher FinLedger created.

One Import into a disposable company answers all four.

## 2. What it could create in Tally

At most, in the target company only:
- **one Purchase voucher** (party ledger and purchase ledger chosen below, one amount, narration with
  `finledger:document_id=<uuid>`, reference `FL-LIVE-<yyyymmdd>-1`);
- **one new bill-wise reference** of that name under the party ledger, if bill-wise is enabled for it;
- the next voucher number in the Purchase series, and updated ledger balances.

It must **not** create ledgers, groups, stock items, cost centres or companies (P4 never sends masters; the Import
guard accepts exactly one `VOUCHER ACTION="Create"`). A rejection creates nothing. A timeout may or may not have
created the voucher; that is the point of section 9.

## 3. Use a disposable test company

- A person creates a **new company by hand** in TallyPrime, named `FinLedger Live Test <yyyymmdd>`, in a data folder
  used for nothing else. Do **not** use `FinLedger Test Company` (it holds the existing test voucher, which must not
  change) or any client company.
- In it, the person creates two ledgers by hand: `FinLedger Live Vendor` (Sundry Creditors) and
  `FinLedger Live Purchase` (Purchase Accounts). No GST, no TDS.
- Load **only** this company in Tally for the whole test (Alt+F3 > Shut every other company). One loaded company
  removes any chance of resolving another one.

## 4. Verify the target company (read-only)

1. `python -m finledger_tally health` with `FINLEDGER_TALLY_COMPANY="FinLedger Live Test <yyyymmdd>"` and no GUID set.
   Expect `company_verified`, `companies_loaded: 1`, `masters: ok`, `ledger_count: 2` (plus Tally's defaults, e.g.
   Cash and Profit & Loss A/c).
2. Write down `company_guid`. Set `FINLEDGER_TALLY_COMPANY_GUID` to it and run `health` again: exit 0.
3. Run the read-only voucher capture (`Run-FinLedger-Capture.cmd` from the kit) with only this company loaded:
   **zero vouchers**. If any voucher
   exists, stop: it is not a fresh company.

## 5. Confirm it cannot be a production company

All must hold, checked by the person, before step 7:
- the company name starts with `FinLedger Live Test` and was created today, by this person, for this test;
- its GUID differs from every GUID ever recorded for a client (the platform's `erp_links`, the earlier captures);
- the voucher count from step 4.3 was zero;
- only this company is loaded;
- the agent's `FINLEDGER_TALLY_CLIENT_ID` is a **test client** created on a scratch platform database (section 6),
  never a real client id.

## 6. Verify the approved document

The job must come from a **real P3 approval**, not hand-built authorization rows:
1. On a scratch Postgres with P2's migrations (as in `tests/test_p3_integration.py`), create a test client, score and
   submit a purchase draft with the two ledgers above, amount `100.00`, GST `exempt`, no TDS, bill-wise `new_ref`
   `FL-LIVE-<yyyymmdd>-1`, then `approve(post=True)` by a second user (maker-checker).
2. Read the post job and the rows `PostAuthorization.from_rows` needs. Record: `document_id`, `revision`,
   `idempotency_key`, `request_hash`, approval kind and revision.
3. `check_authorization(job, auth, settings)` must return `[]` with posting still **disabled** except for
   `posting_disabled`. Any other problem: stop.

## 7. Run the preflight and record the request digest (read-only)

Save the job and the P2 rows from section 6 as one JSON file (`client_id`, `job`, `document`, `attempt`,
`approvals`), set `FINLEDGER_TALLY_LIVE_TEST_COMPANY_GUID` to the GUID from section 4, keep posting **disabled**, and
run:

```
python -m finledger_tally preflight live-test.json
```

It sends nothing. Exit 0 means every check passed; exit 2 lists the failed checks (stop); exit 1 means the file is
unusable. The checks, in order: `disposable_company_designated` (the env GUID is canonical and equals the paired
GUID), `posting_state` (still disabled), `company_name_convention` (starts with `FinLedger Live Test`),
`authorization` (only `posting_disabled` allowed), `human_approval` (a policy approval is not enough),
`fresh_document` (no journal entries), `tally_reachable`, `single_company_loaded`, `company_verified` (pinned, name
and GUID checked before and after), `company_has_no_vouchers`, `preview`, `preview_deterministic` (two builds,
identical bytes) and `marker_absent`. A ready report prints the request `sha256` and the confirmation phrase
`IMPORT ONE VOUCHER <document_id> INTO <company_guid> SHA256 <first 16 hex>`.

Also record `preview.sha256` and the full request XML (`preview_purchase_xml` with the fetched masters). A person reads the XML: one voucher, the two ledgers, `100.00` on each
side with opposite signs, the reference, the marker. Then confirm the marker is **not** in Tally (`lookup_document`
→ `not_found`). Both are read-only.

## 8. Send exactly once and capture the complete response

1. Set `FINLEDGER_TALLY_POSTING=enabled` for **one process**, call
   `execute_live_import(job, auth, settings, disposable_company_guid=<guid>, confirmation=<the phrase, typed>)` once,
   and unset it again immediately. No loop, no agent, no retry. It re-runs every section 7 check with posting enabled
   and refuses (nothing sent) on any failure or on a phrase that does not match these exact bytes. It then calls
   `post_approved` once with `expected_request_sha256` set to the confirmed digest, so a preview that changed since
   the preflight is BLOCKED (`request_changed`) before anything leaves. It never retries: an uncertain result comes
   back as `outcome_uncertain`, and calling it again is refused (`fresh_document` fails).
2. The journal folder `state\journal\<document_id>\` now holds `000N_request.xml`, `000N_response.xml` (the exact
   bytes Tally returned) and `journal.jsonl`. Copy the whole folder before doing anything else; record
   `request_sha256` (must equal step 7) and `response_sha256`.

## 9. Tell success, rejection and uncertainty apart

| Result | Means | Next |
|---|---|---|
| `posted` | reply said exactly one created **and** a fresh lookup found exactly one voucher with the marker, reference and amount | record `erp_id` (GUID), MASTERID, ALTERID; open the voucher in Tally and compare |
| `tally_rejected` | reply said nothing created, with errors, **and** the lookup did not see it | keep the reply; fix the cause through P3 as a new revision; never resend this one |
| `outcome_uncertain` | anything else: timeout, reset, unknown reply, created-but-not-found, rejected-but-found | section 10 |
| `send_not_delivered` | the connection was refused before any byte left | nothing was sent; the whole test may be repeated later from step 4 |

HTTP 200 alone is never success; neither is a `CREATED` counter without the lookup.

## 10. Timeouts and uncertain outcomes

- The document is now blocked in the journal. **Do not send again**, do not clear it.
- Run `reconcile(document_id, date, settings)` (read-only). `reconciled` with a GUID settles it. `not_found` is not
  proof of absence (the date filter is ignored, a filter could change, the voucher could be renamed).
- A person opens Day Book in the test company for the voucher date and looks for the reference and the marker.
- Keep the raw response (even partial) for the parser fix.

## 11. Why the request must never be blindly retried

Tally's XML Import has no idempotency key. If the first request created the voucher and only the reply was lost, a
second identical request creates a **second voucher**: a duplicate purchase in the books. FinLedger therefore sends
at most once per document and blocks after any doubt; only a person who has checked Tally may clear the block
(`clear_for_resend`, with their name and a note), and even then the next send repeats the marker lookup first.

## 12. Reconcile the resulting voucher

After `posted` or `reconciled`: export the voucher (capture kit or `lookup_document`), confirm its GUID starts with the
company GUID, MASTERID/ALTERID, reference, amount, party, and that the narration still carries the marker. Report
the outcome to the scratch P3 with `erp_result(**out.erp_result())` and confirm the attempt shows `posted` with the
same `erp_id`. Deliver the journal folder, the two XML files, the voucher export and the scratch P3 rows.

## 13. Cleanup (only if separately authorized)

Default: **leave everything in place.** The disposable company exists only for this test. If a person later
authorizes cleanup, they delete only the one voucher identified by the recorded GUID / MASTERID, by hand in Tally,
or delete the whole disposable company. FinLedger never deletes or alters vouchers; there is no code for it.

## Stop rules (any one stops the test, nothing is sent after it)

- `health` is not exit 0, or shows more than one loaded company, or the GUID changes between steps.
- The company has any voucher before the test, or its name/GUID could belong to a client.
- `check_authorization` reports anything except `posting_disabled` before enabling.
- The preflight (section 7) is not exit 0, or any of its checks fails when re-run at send time.
- The request digest in step 8 differs from step 7 (the code refuses this itself: `request_changed`).
- The person at the PC is unsure at any point.
