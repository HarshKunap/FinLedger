# FinLedger — Release integration assignment

Prepared 2026-10-22. Repository: https://github.com/ryanshaon/FinLedger

## 1. Your objective

Own the remaining P1–P3 integration, staging setup, controls/UI regression work and release
verification. Reuse existing implementations; deliver a reproducible synthetic invoice journey
from upload to human review. Customer launch is a separate owner decision. Do not edit `person4_tally/`.

This brief is the current assignment for its recipient, including controls/QA previously listed
under Ryan. It does not cancel work already accepted by another contributor. Before coding,
agree named owners and PR links for overlapping items in [NEXT_PERSON_HANDOVER](../NEXT_PERSON_HANDOVER.md).
If that friend is building the migration/consumer, integrate and verify their PRs instead of
creating competing versions. Take over an item only after explicit coordination.

## 2. Read first and establish the baseline

Read [HANDOVER](../../HANDOVER.md) completely for mandatory safety rules and owner gates,
then [the AI handover](../NEXT_PERSON_HANDOVER.md),
[the schema proposal](../proposals/AI_DURABLE_RETRIEVAL_USAGE_SCHEMA.md),
[the deployment runbook](../DEPLOYMENT_RUNBOOK.md),
[Auth design](../plans/AUTH_SUPABASE_DESIGN.md) and
[provider proposal](../decisions/ADR-001-AI_PROVIDER.md).
Older handover/audit statements that no provider or durable adapters exist are superseded by
PR #37: the adapters exist, but live network transport and production integration remain unfinished.

Latest inspected main before this brief: `cf5d2f0`, PR #37. Its October 20 CI reported
**71 AI / 333 platform / 55 UI tests passed**, mock **3/3 structural + 6/6 exact**.
These are recorded CI results, not a new local run or proof of hosted configuration.

Use Python 3.12, uv and PostgreSQL tools; Docker is needed for the Compose stack. From your clone:

```powershell
git switch main
git pull --ff-only
git switch -c feat/release-integration-baseline
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

Record actual results, tool/setup gaps and skipped checks. Never use a hosted database as the test-owner DSN.

## 3. AI runtime integration

- [ ] Inspect the existing Gemini adapter, `GeminiTransport` interface, fake transport,
  `PostgresIndexer`, `PostgresKillSwitch`, `PostgresUsageWriter` and extraction contract.
- [ ] Prepare network-transport tests with fake HTTP, fixed diagnostic categories, explicit
  location/model, timeouts, bounded retries/payloads and safe authentication handling.
- [ ] After remaining policy gates are approved, complete the actual provider transport and
  runtime wiring. Do not simply remove the staging/production guard to get a demo running.
- [ ] Bind durable retrieval/accounting to the runtime using the reviewed platform schema;
  prove restart recovery, non-owner RLS, cross-tenant denial and concurrent reservation limits.
- [ ] Keep live evaluation opt-in. Run synthetic live extraction only with explicit approval;
  record model/location, measured usage/cost and remaining limitations without invoice payloads.

**Acceptance:** no silent fallback to mock or another provider/location; no provider call before
policy/configuration validation; durable accounting cannot be bypassed by restart or another worker.
Revalidate the dated model/lifecycle/pricing proposal before any approved activation.

## 4. Integrate the platform handover

Coordinate these tasks with the contributor already assigned the AI handover. Own integration
and evidence; do not duplicate their migration or worker.

- [ ] Review/merge the next numbered platform migration for tenant-safe retrieval, usage and
  atomic reservations. Test against fresh and upgraded local databases. Test-only overlay SQL
  is not a deployment migration; inspect existing rows before adding uniqueness constraints.
- [ ] Integrate `extract-worker` using existing claims/leases, tenant context, input loader and
  `extract_document(...)`; wire successful results into scoring/mapping and human review.
- [ ] Ensure statements become explicit exceptions without a model call; reject object keys
  outside the current tenant prefix before storage reads or AI invocation.
- [ ] Implement or integrate one non-login automation maker per firm: no Auth subject, bearer
  token or admin privilege. Validate human-editor/maker-checker separation with an independent reviewer.
- [ ] Prove idempotent replay, compare-and-set status updates, bounded retries/dead letters,
  lease expiry/late-result rejection, atomic settlement and invoice-free queue diagnostics.

**Acceptance:** synthetic intake reaches review once, without tenant leakage or duplicate scoring.
Failure/recovery cases have regression tests. Extraction never authorizes posting or payment.

## 5. Staging operations and dependency proofs

- [ ] Verify what is actually deployed before changing it; current hosted state has not been
  freshly inspected for this brief. Preserve existing data and private configuration.
- [ ] With owner-approved access, verify/create the restricted runtime login and appropriate
  worker grants using the session pooler. Never run application queries as owner/service role.
- [ ] With private owner configuration, prove S3 put/get/presigned access, unsigned-public denial
  and cleanup. Record leftovers explicitly; never clear a bucket or delete volumes as cleanup.
- [ ] Verify ClamAV readiness and fail-closed behavior; configure safe ingress/proxy logging,
  HTTPS, request limits, stable encryption/signing keys and worker operational checks.
- [ ] Test Resend delivery and inbound webhooks with synthetic messages after approval;
  do not blindly replay outbox items or expose signed/invite URLs in logs.
- [ ] Deploy API, control UI and separate workers only to an approved hosting account/budget.
  Document immutable image version, dependency checks, monitoring and safe rollback.

**Acceptance:** dependency proofs and cleanup have actual results; health endpoints alone do not
count as storage/Auth/email/scanner verification. Document any hosted configuration/SQL changes.
Migrations 001–011 were already applied in the last verified staging state: do not reapply/edit
them. Commit exact new migration SQL and obtain review before approved hosted execution.

## 6. Controls, UI and end-to-end QA

- [ ] Own review/correction/rejection, duplicate-submission and invalid-form regression tests.
  Make small usability/error-display fixes; preserve authorization and workflow rules.
- [ ] Present queued, failed, budget-exhausted and excluded-statement states accurately;
  never show processing success solely because a job was accepted.
- [ ] Verify invite → set password → login → TOTP → client list → logout, first with fake Auth,
  then synthetic staff on approved staging. Test AAL1 denial, old-cookie rejection/revocation,
  cross-client denial and 8-hour absolute / 30-minute idle session expiry. SSO remains deferred.
- [ ] Run synthetic upload → extract → score/map → correction/review with negative cases:
  scanner outage, provider timeout, malformed output, cap exhaustion, lost lease and retries.
- [ ] Record bugs with steps/expected/actual result and redacted evidence; fix affected callers,
  fixtures and tests together. Obtain independent review for security-sensitive changes.

**Acceptance:** reproducible browser/workflow tests and a release evidence report with exact
pass/fail/skip counts, commit, environment, cleanup, unresolved blockers and rollback instructions.

## 7. Work order and completion rules

1. Baseline tests and overlap agreement; start local transport/control regression work.
2. Integrate the assigned friend's reviewed schema/consumer; wire adapters and maker behavior.
3. Resolve owner gates, provision only approved resources, then verify staging dependencies/Auth.
4. Complete invoice/failure E2E, fix regressions, request independent review and deliver release evidence.

Work in small branches/PRs; green CI must cover the exact integrated commit. Coordinate shared
contracts, migration numbers and root configuration. P2 dependency changes require both P2/P3 lockfiles.
Update HANDOVER with exact results and unfinished branches before stopping. Never commit secrets,
share `.env`, weaken RLS/MFA/scanning, force-push shared history or add attribution trailers.

Keep the existing layout: `packages/` + `evals/` for AI, `person2_platform/` for runtime/migrations,
`person3_control_ui/` for controls and tests, and `docs/` for plans/evidence. No new parallel architecture.
Defer speculative indexing until real query plans exist. Person 4 stays separately owned and untouched.
