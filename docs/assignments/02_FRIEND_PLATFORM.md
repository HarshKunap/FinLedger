# Friend 2 — Platform, integration and deployment

## Objective

Connect the implemented platform to approved services and the AI pipeline. This assignment
needs the strongest backend/operations experience. Ryan owns controls/testing; Friend 1 owns AI.

Repository: https://github.com/ryanshaon/FinLedger

## 1. Read and set up

Read root `HANDOVER.md` completely, then `docs/TEAM_WORK_SPLIT.md`,
`docs/DEPLOYMENT_RUNBOOK.md`, `docs/plans/AUTH_SUPABASE_DESIGN.md` and the consumer design
in `docs/audits/PERSON1_PRODUCTION_READINESS.md`. HANDOVER is authoritative for hosted state.
Use Python 3.12, uv and PostgreSQL tools. From the repo root:

```powershell
git switch main
git pull --ff-only
git switch -c feat/platform-integration-tests
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

## 2. Your files and work

Own `person2_platform/`, Docker/Compose and integration configuration. Coordinate root CI,
shared contracts and lockfiles rather than changing another contributor's interfaces alone.

- [ ] First deliverable: verify existing local stack and queue replay/lease behavior;
  record actual scanner readiness, failures and cleanup, not just health endpoints.
- [ ] Own migration numbering/review for AI persistence with Friend 1.
- [ ] Once remaining handover gates are resolved, build the missing extraction consumer
  using existing queue leases, tenant context, input loaders and safe diagnostic patterns.
- [ ] Implement the selected per-firm non-login automation maker and statement-exception
  path; coordinate maker/checker semantics with Ryan and get security review.
- [ ] Prove idempotency, retry/dead-letter paths, late-result lease loss, tenant-prefixed
  storage access and atomic status/result settlement. Coordinate extract-to-score integration.
- [ ] With private owner setup/approval, create the least-privilege runtime login and prove
  storage access, unsigned denial, cleanup, scanner behavior and email/webhook delivery.
- [ ] Deploy API/UI/workers only to an approved account/budget; document rollback and logs.
  Give Ryan a test URL and synthetic test accounts through a secure channel, not shared secrets.

## 3. Dependencies and limits

Hosted migrations 001–011 are already applied. Never reapply/edit them; new SQL must be
committed and reviewed before approved hosted execution. Runtime must not use an owner,
service-role or RLS-bypassing database login. Use the documented session pooler.

Owner still supplies private credentials, app-login password, Auth settings, sender domain,
hosting approval/budget and AI policy approval. Friend 1 supplies the provider integration.
No automatic posting/payment permission follows from the automation-maker choice.

## 4. Done and delivery

Done means synthetic ingest → extraction → scoring/review works under non-owner RLS;
retry/recovery, dependency/privacy/cleanup proofs and deployment/rollback evidence are recorded.
Each PR needs exact test commands/counts, blockers and hosted changes (or none), with green
CI on the exact commit. P2 dependency changes require updating/checking both P2/P3 lockfiles.

Never expose/commit `.env`, credentials or invoice data. Never delete volumes to fix startup,
weaken scanning/Auth, force-push main or edit `person4_tally/`. No AI attribution trailers.
Update HANDOVER; Tally implementation/testing is outside this assignment.
