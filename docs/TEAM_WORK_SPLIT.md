# FinLedger: remaining work for three contributors

Prepared 2026-10-17. Scope: Persons 1–3, not three new implementations.
Reuse the existing code. The core platform/UI is implemented; live AI, durable AI state,
service configuration and end-to-end release proofs remain unfinished.

## 1. Start here

Share one assignment per person: [Friend 1 — AI](assignments/01_FRIEND_AI.md),
[Friend 2 — Platform](assignments/02_FRIEND_PLATFORM.md),
[Ryan — Controls and QA](assignments/03_RYAN_CONTROLS_QA.md).
Ryan's role has the easiest starting tasks; security-sensitive changes still require review.

Read [HANDOVER](../HANDOVER.md) completely, then the documents linked in your assignment.
It remains the authority for owner decisions, hosted state and safety rules.

```powershell
git clone https://github.com/ryanshaon/FinLedger.git
cd FinLedger
git switch main
git pull --ff-only
git switch -c feat/your-small-task
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

Prerequisites: Python 3.12, uv and PostgreSQL tools on PATH. See [README](../README.md).
Use synthetic invoices only. Never distribute `.env`; copy `.env.example` only if `.env`
does not exist. The last recorded P1–P3 baseline is **43 / 333 / 55 tests** plus mock
evaluations **3/3 structural and 5/5 exact**. These are historical counts, not a fresh run.

| Contributor | Responsibility | Primary files | First deliverable without live credentials |
|---|---|---|---|
| A — AI owner | Extraction/provider, durable retrieval and usage limits | `packages/`, `evals/` | Regression fixtures and an interface/persistence proposal against the audit |
| B — Platform owner | Queue integration, DB migrations, storage/email and deployment | `person2_platform/`, `Dockerfile`, `compose.yaml` | Queue replay/lease test plan and local stack verification |
| C — Controls and QA owner | Human-review controls, browser Auth proofs, release evidence | `person3_control_ui/` | Synthetic review/Auth regression scenarios and acceptance checklist |

These assignments are proposed ownership, not a claim that friends have accepted them.
Owner account access is not required for the first deliverables. No one edits `person4_tally/`.

## 2. Contributor A: AI and durable state

Read the [AI audit](audits/PERSON1_PRODUCTION_READINESS.md) and
[provider proposal](decisions/ADR-001-AI_PROVIDER.md).

- [ ] Trace existing provider, extraction, retrieval, usage and cap interfaces; reuse them.
- [ ] Prepare synthetic PDF/image fixtures and tests for schema failures, bounded images,
  minimized input and content-free diagnostics. Do not add a second extraction architecture.
- [ ] Propose tenant-scoped persistence for retrieval, usage and atomic budget reservations.
  Agree the SQL schema with B before either contributor writes overlapping migrations.
- [ ] After the remaining policy gates are approved, implement the approved provider adapter
  with explicit model/location, timeouts, bounded retries and no silent provider/region fallback.
- [ ] Implement persistent retrieval and accounting against B's reviewed schema. Prove
  restart recovery, cross-tenant denial, concurrent reservations and kill-switch enforcement.
- [ ] Supply B a tested extraction contract, failure categories and measured synthetic results.

**Done means:** provider contract tests and real synthetic staging proof after approval;
durable state survives restart; concurrent workers cannot bypass caps; no invoice content
in logs. Mock evaluations alone do not satisfy this acceptance gate.

**Blocked:** live calls/activation require owner-approved provider, processing location,
effective retention controls, budget and account/capacity. Do not infer approval from research.
Model availability and prices in the dated ADR must be revalidated before activation.

## 3. Contributor B: platform, worker and deployment

Read the [deployment runbook](DEPLOYMENT_RUNBOOK.md), the audit's consumer design and
the [Auth design](plans/AUTH_SUPABASE_DESIGN.md).

- [ ] Verify the existing local stack; record Docker/scanner startup failures rather than
  claiming a health endpoint proves all dependencies. Do not delete persistent volumes.
- [ ] Coordinate A's schema needs and own migration numbering/review. Hosted migrations
  001–011 are already applied; never reapply or edit them. Commit new SQL before hosted use.
- [ ] Agree with A/C the extract-to-score boundary and corrected-invoice maker/checker behavior.
  Once handover policy gates are resolved, build the extract consumer using existing queue
  claims, leases, tenant context and storage loaders. No live calls before approval.
- [ ] Implement the selected per-firm non-login automation maker and statement-exception path
  with C reviewing authorization and separation-of-duties semantics.
- [ ] Prove retry/dead-letter handling, idempotent replay, lease loss, tenant-prefixed object
  access and atomic status/result settlement. Never settle work owned by a newer claimant.
- [ ] With owner-entered private credentials and explicit hosted approval: create the restricted
  runtime login, run storage/privacy/cleanup proofs, configure scanner and test email delivery.
- [ ] Deploy API, UI and workers only to an approved hosting account/budget; document rollback,
  private logging configuration and dependency checks. Give C the staging URL, not secrets.

**Done means:** ingest → extraction → scoring/review integration works on synthetic data;
runtime uses a non-owner RLS-enforced login; dependency proofs and cleanup are recorded;
deployment and rollback evidence exists. No Tally posting is part of this assignment.

**Blocked:** owner credentials, Auth configuration, app-role password, sender-domain access,
hosting account/budget, and A's approved provider integration. Prepare locally; don't bypass gates.

## 4. Contributor C: controls, browser flow and release QA

Read the [Auth design](plans/AUTH_SUPABASE_DESIGN.md), Person 3 architecture and existing tests.

- [ ] Preserve existing risk/workflow controls; add synthetic fixtures for corrected invoices,
  duplicate submissions, rejection and unauthorized cross-client requests.
- [ ] Review B's automation-maker integration: automation has no Auth subject/API token/admin
  privilege; it must not let a human editor approve their own work. No new automatic-posting authority.
- [ ] Verify statement-exclusion and extraction failure states are understandable in the UI.
- [ ] Prepare browser acceptance tests for invite → password → login → TOTP → client list,
  logout/revocation, expiry, invalid inputs and cross-tenant denial; fake Auth locally first.
- [ ] After owner setup and B's staging runtime, run the real synthetic-staff Auth flow. Prove
  AAL1 cannot read client data and old cookies cannot bypass MFA or session revocation.
- [ ] Run the integrated synthetic invoice path from upload to human review with A/B; exercise
  timeouts, scanner outage, exhausted caps, retry exhaustion and recovery, not just the happy path.
- [ ] Publish redacted release evidence: commit, commands, exact pass/fail/skip counts,
  environment, cleanup and remaining blockers. Keep customer intake disabled until approved.

**Done means:** reproducible browser and review proofs, including negative authorization cases;
no misleading success when jobs are queued/failed; an explicit release checklist rather than a
claim of production readiness. Live email/model/Tally operations are not implied by QA ownership.

**Blocked:** actual invite/MFA testing needs owner Auth settings/private keys and B's runtime.
Complete fake-provider/local tests first; never weaken authentication to make a demo work.

## 5. Shared boundaries and owner actions

| Shared surface | Coordination rule |
|---|---|
| `packages/contracts/`, `shared/`, queue payloads | A proposes AI fields; B/C review consumers before merge. Update callers, fixtures and tests together. |
| Migrations and role grants | B is the single coordinator; A/C request changes. Test non-owner RLS and commit exact SQL before any approved hosted execution. |
| Dependencies/lockfiles | B coordinates; changing P2 dependencies requires updating/checking both P2 and P3 locks. |
| Root CI, Docker, Compose | B owns integration; C checks coverage. Current root CI covers P1–P3 and the shared image, not a Person 4 release proof. |
| README and HANDOVER | Task author updates relevant status; owner/integrator resolves overlaps. Never erase historical evidence. |

**Already decided — don't ask again:** password + TOTP, SSO deferred, 8-hour absolute /
30-minute idle staff sessions; one non-login automation maker per firm; exclude statements
from v1; minimized invoice text and bounded page images under the proposed controls.
Serve India and EU; processing locations are deferred, not implicitly approved for cross-border transfer.

**Owner still supplies/approves:** private storage/Auth/email credentials and domain setup;
restricted app-login password; hosting account/budget; AI provider/location/retention/budget/capacity.
Friends should receive least-privilege access where needed, never a shared `.env` in Git or chat.

## 6. Implementation order and pull-request checklist

1. All three run local tests and trace their owned code; A/B agree contracts/schema, B/C agree controls.
2. Parallel local preparation: A fixtures/design, B runtime/queue proofs, C browser/control tests.
3. Owner resolves gates; A delivers provider/persistence and B integrates the consumer in small PRs.
4. B deploys approved staging; C leads Auth and invoice E2E with A/B fixing their own failures.
5. Owner reviews release evidence; customer launch remains a separate approval, not an automatic next step.

Every PR must include scope, tests with exact results, blockers and hosted changes (or explicitly none).
Run `git diff --check`, inspect the diff for secrets/unrelated files, and require green CI on the
exact commit before integration. No force pushes, mass cleanup, dependency upgrades or branch deletions
as part of onboarding. Each contributor uses their own Git identity; agents in this owner's checkout
follow HANDOVER's owner-identity rule. No AI attribution trailers.

Existing repository structure stays unchanged:

```text
packages/ + evals/                     A: AI implementation and fixtures
person2_platform/                     B: platform, queue, migrations
person3_control_ui/                   C: controls and browser tests
docs/TEAM_WORK_SPLIT.md                assignments (this file)
docs/DEPLOYMENT_RUNBOOK.md             operational checks
docs/audits/ + docs/decisions/         gaps and proposed decisions
HANDOVER.md                           current state and mandatory rules
person4_tally/                        separate owner; no changes by this team
```
