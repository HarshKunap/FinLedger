# Person 4: contract gaps, one by one (2026-10-11, release-readiness pass)

What Person 4 needs from the shared contracts (Persons 2 and 3, `packages/contracts`) and from outside evidence.
**No Person 1/2/3 code was changed.** Every B/C item is a proposal for its owner. Each gap that depends on an
upstream definition is pinned by a test in `tests/test_contract_compat.py`: if upstream changes, that test fails and
names the gap, so the change is noticed instead of silently ignored.

**Owner categories:** A = solvable inside P4 · B = P2 contract or persistence · C = P3 approval/review · D = another
platform component · E = needs a real Tally capture · F = needs a product or accounting policy decision.

## Summary

| # | Gap | Owner | Status | Evidence |
|---|---|---|---|---|
| 1 | No uncertain posting state on the attempt | B | **open**; P4 compensates locally | `test_gap1_…` |
| 2 | No request/response evidence on the attempt | B | **open**; evidence in the local journal only | `test_gap2_…` |
| 3 | `erp_links` has no company GUID | B | **open**; GUID from config | `test_gap3_…` |
| 4 | Post job lacks invoice total and vendor GSTIN | C | **open**; optional P4 parameters | `test_gap4_…` |
| 5 | Amounts are floats in shared schemas | B/C | **partly fixed upstream** (P3 boundary is `Decimal`); shared schema and P2 `OpenBill` still float | `test_gap5_…`, `test_gap16_…` |
| 6 | GST tax lines untyped | C + F | **open**; GST refused | `test_gaps_6_7_…` |
| 7 | TDS untyped | C + F | **open**; TDS refused | `test_gaps_6_7_…` |
| 8 | Debit/credit notes cannot name original invoice or issuer | C + F | **open**; notes refused | `test_gap8_…` |
| 9 | Due date vs Tally credit period | F + E | **open**; due date never invented | `test_gap16_…` |
| 10 | No agent route to claim post jobs / report results | B | **open**; interfaces only, no `run` command | `test_gap10_…` |
| 11 | `claim_job` re-leases an expired post job | B | **partly fixed upstream** (`claim_token` fences a stale `finish_job`); re-lease still happens | `test_gap11_…` |
| 12 | No attachment Tally can store | D + E | **open**; narration marker only | — |
| 13 | P3 voucher types differ from the shared enum | C | **open**; non-purchase refused | `test_gap13_…` |
| 14 | Approval for the exact job | A | **done in P4** (now also document, client and supersession) | `test_release_boundary.py`, `test_p3_integration.py` |
| 15 | ERP id already recorded by P3 | A | **done in P4** | `test_hardening.py`, `test_p3_integration.py` |
| 16 | Open-bills payables rule and sign | E + F | **interim rule in P4**; live check pending | `test_bills.py` |
| 17 | Import reply shape | E | **open**; strict parser, unclear = uncertain | `test_posting.py` |
| 18 | Bill reply shape | E | **open**; `Run-Bills-Capture.cmd` ready | `test_bills_capture.py` |
| 19 | Isolation with two populated companies | E | **partially verified** | `test_isolation_check.py` |
| 20 | Service wrapper, install path, pairing-token storage | D + F | **open**; assessed in `WINDOWS_DEPLOYMENT.md` | — |

## Upstream changes already in the repository (recognised, not re-proposed)

| Upstream change | Where | Effect on P4 |
|---|---|---|
| `approvals.superseded_at` / `superseded_by_revision` | P2 migration 005, written by P3 `edit_field` | P4 now reads `superseded_at`; a superseded approval never counts (`approval_superseded`) |
| `jobs.state` may be `cancelled`; P3 cancels a queued post job on an edit after approval | migration 005, P3 `edit_field` | the `posting_attempts` row stays `pending`, so P4 must not rely on it alone; document status + supersession refuse it (real-DB test) |
| `claim_token` and `finish_job(..., p_claim_token)` | migration 005 | a stale holder cannot finish a re-leased job; does not stop the re-lease itself (gap 11) |
| Tenant foreign keys `(client_id, document_id)` on jobs, approvals, attempts | migration 005 | rows of one document always share one client; P4 also checks this itself in `from_rows` |
| P3 boundary amounts are `Decimal` | `finledger_control/boundary.py` | half of gap 5; P4 still re-reads every amount as an exact string |

---

## The 20 gaps

Each entry: the missing piece · who produces and consumes it · whether P4 can fix it · the proposal (current,
proposed, semantics, compatibility, tests, unlocks, migration) · status.

### 1. Uncertain posting state
- **Missing:** `posting_attempts.result` allows only `pending | posted | failed`. P4's `outcome_uncertain` (an
  Import may have reached Tally) has nowhere to go.
- **Producer / consumers:** produced by P4 (via P3 `erp_result`); consumed by P3 (exception tray, re-approval) and
  P2 (the attempt row).
- **P4 can fix?** No. P4 compensates: the local journal blocks any resend, and P3 receives `ok=False` with error text
  starting `outcome_uncertain (reconciliation required; FinLedger will not resend)`.
- **Current:** `check (result in ('pending','posted','failed'))`; `erp_result(ok=False)` → `failed`, document →
  `exception`. A later re-approval makes a new revision, a new job and a new attempt.
- **Proposed:** add `outcome_uncertain` to the check; `erp_result(..., uncertain=True)` sets it, moves the document to
  `exception` with reason `reconciliation_required`, and refuses to enqueue another post job for the document until a
  reviewer records a reconciliation decision (`reconciled_posted(erp_id)` or `reconciled_absent(note)`).
- **Semantics:** `outcome_uncertain` never becomes `failed` automatically; only `posted` (with an erp_id) or a
  recorded human decision leaves it.
- **Compatibility:** additive enum value. Readers that switch on `result` must treat unknown values as blocking.
- **Tests:** `erp_result(uncertain=True)` then `approve(post=True)` creates no new post job; a later `posted`
  callback is accepted only with an erp_id. P4 side: `test_gap1_…` flips, and P4 then sends `uncertain=True`.
- **Unlocks:** the platform itself blocks a resend even if the agent's state folder is lost.
- **Migration:** `alter table posting_attempts drop constraint …; add constraint … check (result in
  ('pending','posted','failed','outcome_uncertain'))`; no backfill (no live posts exist).
- **Status:** open.

### 2. Posting evidence on the attempt
- **Missing:** no place for the request digest, response digest or a bounded reply excerpt.
- **Producer / consumers:** P4 produces (`PostOutcome.request_sha256/response_sha256`); P3 review UI and auditors
  consume.
- **P4 can fix?** No; P4 keeps the exact bytes and digests in the local journal.
- **Proposed:** `posting_attempts.request_sha256 text`, `response_sha256 text`, `response_excerpt text` (≤ 4 KB,
  Tally error lines only, no narration), passed through `erp_result`.
- **Compatibility:** nullable, additive. **Tests:** `erp_result` stores and returns them; P4 `ResultSink` fills them.
- **Unlocks:** audit without access to the PC. **Migration:** three `add column` statements.
- **Status:** open.

### 3. Company GUID on the ERP link
- **Missing:** `erp_links` has `company_name` only. P4 trusts the GUID, never the name.
- **Producer / consumers:** P4 health produces `company_guid`; P2 pairing stores it; P4 agent config reads it.
- **P4 can fix?** No; today the GUID is typed into `FINLEDGER_TALLY_COMPANY_GUID` at pairing.
- **Proposed:** `erp_links.company_guid text check (company_guid ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')`,
  written once at pairing from `health().company_guid`; the claim route (gap 10) refuses to hand out post jobs while
  it is null; changing it requires re-pairing.
- **Compatibility:** nullable, additive. **Tests:** pairing stores it; claim refuses when null; P4 refuses a claim
  whose GUID differs from its config.
- **Unlocks:** no typed GUID; the platform can refuse jobs for a re-paired company. **Migration:** one column.
- **Status:** open.

### 4. Invoice total and vendor GSTIN in the post job
- **Missing:** the job carries only the draft, so P4 cannot cross-check the draft total against the invoice or the
  party ledger against the GSTIN.
- **Producer / consumers:** P3 `_enqueue_post` produces; P4 consumes.
- **P4 can fix?** Partly: `expected_total` and the GSTIN check exist as optional parameters; nothing supplies them.
- **Proposed:** payload keys `invoice_total` (decimal string from `canonical_invoices.payload.total`) and
  `vendor_gstin` (string or null), included in `request_hash` like every other key.
- **Compatibility:** additive; older jobs lack them and P4 keeps today's behaviour. **Tests:** P4 `expected_total`
  mismatch → `validation_rejected`; `test_gap4_…` flips.
- **Unlocks:** an end-to-end amount check. **Migration:** none (new jobs only).
- **Status:** open.

### 5. Exact amounts
- **Missing:** shared `VoucherLine.amount`, `TdsDetails.amount` and P2 `OpenBill.amount/pending_amount` are `float`.
- **Producer / consumers:** P1/P3 produce drafts; P2 stores bills; P4 consumes and produces bills.
- **P4 can fix?** Only defensively: P4 re-reads every amount as `Decimal` from its string form, refuses more than two
  decimals and never rounds.
- **Already upstream:** P3's `boundary.py` uses `Decimal`.
- **Proposed:** shared schema amounts as decimal strings matching `^-?\d{1,12}(\.\d{1,2})?$`; P2 `OpenBill` amounts
  as `Decimal` serialised as strings.
- **Compatibility:** schema version bump; JSON readers that expect numbers must accept strings.
- **Tests:** `test_gap5_…` and `test_gap16_…` flip; P4 bill payload test switches to strings.
- **Migration:** none for stored JSON (new writes only).
- **Status:** partly fixed upstream.

### 6. GST lines · 7. TDS
- **Missing:** `GstDetails.tax_breakup` is `Dict[str, float]` with no ledger per tax; `TdsDetails` has no rate or base.
- **Producer / consumers:** P1 mapper / P3 review produce; P4 consumes.
- **P4 can fix?** No, and P4 will not guess: any GST amount or TDS is refused (`unsupported_tax`).
- **Proposed shapes:** GST `[{ledger, kind: cgst|sgst|igst|cess, rate, amount}]`; TDS `{section, ledger, rate, base,
  amount}`.
- **Needs a decision first (F):** which side TDS is booked on, how round-off is posted.
- **Compatibility:** new optional fields; the old ones are kept until all producers move.
- **Tests:** `test_gaps_6_7_…` flips; P4 then needs golden XML per tax kind (and, before posting, a live test).
- **Status:** open.

### 8. Debit and credit notes
- **Missing:** the draft cannot say which original invoice a note is against, its own number, or who issued it.
- **Producer / consumers:** P3 produces; P4 consumes.
- **P4 can fix?** No. `post_credit_debit` refuses without any network call.
- **Proposed:** `note: {original_invoice_ref, note_number, issued_by: vendor|us}`; policy decision on which party
  raises a debit note.
- **Tests:** `test_gap8_…` flips. **Status:** open.

### 9. Due date and credit period
- **Missing:** Tally's bill-wise credit period is not the same thing as an invoice due date.
- **P4 can fix?** No. P4 reports bills with `due_date: None` and never invents one; the draft's `bill_wise.due_date`
  is not written to Tally.
- **Needs:** a policy decision (F) and a real Bill reply (E). **Status:** open.

### 10. Agent job routes
- **Missing:** P2 exposes `/agent/masters`, `/agent/open-bills`, `/agent/mapping` but no route to claim a post job or
  report its result.
- **Producer / consumers:** P2 produces; P4 consumes; P3 `erp_result` is called by the route.
- **P4 can fix?** No. P4 ships `JobSource` / `ResultSink` interfaces and no `run` command, so no invented queue.
- **Proposed:** `POST /agent/jobs/claim` (agent token) → one `{job_id, claim_token, payload, document, attempt,
  approvals}` with the exact row columns `PostAuthorization.from_rows` reads, for the paired client, leased;
  `POST /agent/jobs/{id}/result` → `{claim_token, ok, erp_id, error, request_sha256, response_sha256}`, which calls
  P3 `erp_result` and `finish_job`.
- **Compatibility:** new routes. **Tests:** P2 route tests; P4 HTTP `JobSource` against the P2 test app.
- **Unlocks:** `AgentHost.run` as a real process (and with it the Windows wrapper). **Status:** open.

### 11. Post-job re-lease
- **Missing:** `claim_job` hands out a `running` job again once its lease expires; for `post` that means a second
  delivery while the first may still be sending.
- **Already upstream:** `claim_token` makes the first holder's `finish_job` a no-op after a re-lease.
- **P4 can fix?** P4 makes the second delivery harmless (journal gate + pre-send lookup; tested: duplicate delivery
  posts once), but the platform still shows a misleading `running` state.
- **Proposed:** for `queue = 'post'`, an expired lease moves the job to a new state `needs_reconciliation` (never
  back to `queued`); `requeue_dead_job` refuses post jobs unless the attempt is `failed` with a recorded decision.
- **Tests:** `test_gap11_…` flips. **Status:** partly fixed upstream.

### 12. Attachments
- **Missing:** nothing Tally can store as an attachment; P4 writes only the narration marker
  `finledger:document_id=<uuid>`. **Owner:** D + E. **Status:** open (low priority).

### 13. Voucher-type values
- **Missing:** shared enum `purchase | debit_note | credit_note | payment`; P3 boundary `purchase | credit_note |
  debit_note | journal`.
- **P4 can fix?** No. P4 posts only `purchase` and refuses everything else (`unsupported_voucher_type`).
- **Proposed:** one enum in `packages/contracts`, imported by P3. **Tests:** `test_gap13_…`. **Status:** open.

### 14. Approval for the exact job (P4, done)
- `PostAuthorization.from_rows` now **requires** the id/client columns of all three row kinds and `superseded_at`;
  rows of two documents or two clients raise `ValueError` (never silently dropped). Posting needs a
  non-superseded `human`/`policy_clear` approval for the job's revision, for the job's document, plus the attempt
  checks (pending, revision, key, `request_hash`). `payment_release` never counts.
- **Observation for P3:** the `policy_clear` path writes revision `1` and enqueues revision `1` literally
  (`test_p3_policy_clear_approval_is_written_for_revision_1`). Consistent today because a new draft starts at 1; if
  drafts are ever re-submitted at a higher revision, this must use the draft's revision.

### 15. ERP id recorded by P3 (P4, done)
- If the attempt is `posted` with an `erp_id` for exactly this job, P4 returns it as `already_posted` without any
  Tally call (real-DB test).

### 16. Open-bills payables rule
- Interim rule in P4: party parent group exactly `Sundry Creditors` and a positive closing balance; everything else is
  kept in `classify()` with a reason. **Needs:** a real Bill reply (E) to confirm the sign, and a policy (F) for
  sub-groups of Sundry Creditors.

### 17. Import reply · 18. Bill reply · 19. Isolation
- Need real captures. 17 is only obtainable by the authorized live test
  ([`LIVE_IMPORT_TEST_PROCEDURE.md`](LIVE_IMPORT_TEST_PROCEDURE.md)); 18 by the read-only `Run-Bills-Capture.cmd`;
  19 by an optional isolation run with two populated companies.

### 20. Service wrapper, install path, pairing-token storage
- Open questions in `context/PERSON_4.md`. Assessed in [`WINDOWS_DEPLOYMENT.md`](WINDOWS_DEPLOYMENT.md); nothing is
  built or installed. Depends on gap 10 (nothing to run without the routes) and gap 3 (pairing).

## Other cross-person observations (not new gaps)

- `finish_job(ok=False)` re-queues with back-off unless `p_permanent`. A future `ResultSink` must finish post jobs
  with `p_permanent=true` for every P4 outcome; P4's journal already blocks a resend if one slips through.
- `POST /clients/{id}/jobs/{job_id}/requeue` can re-queue a dead post job; the same journal and attempt checks apply.
