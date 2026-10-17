# Friend 1 — AI and durable state

## Objective

Finish the existing AI prototype, not rebuild it. This is the AI-focused assignment;
the platform friend owns queue/runtime integration and Ryan owns controls/testing.

Repository: https://github.com/ryanshaon/FinLedger

## 1. Read and set up

Read root `HANDOVER.md` completely, then `docs/TEAM_WORK_SPLIT.md`,
`docs/audits/PERSON1_PRODUCTION_READINESS.md` and `docs/decisions/ADR-001-AI_PROVIDER.md`.
These files travel with the repo; HANDOVER is authoritative for current gates.
Use Python 3.12, uv and PostgreSQL tools. From the repo root:

```powershell
git switch main
git pull --ff-only
git switch -c feat/ai-fixtures
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

## 2. Your files and work

Own `packages/` and `evals/`; coordinate shared contracts with the other two people.

- [ ] Trace the existing extraction, provider, retrieval, usage and token-cap interfaces.
- [ ] First PR: synthetic invoice fixtures and regression tests for malformed output,
  bounded images, minimized inputs and invoice-free diagnostics. No real provider calls.
- [ ] Agree durable retrieval, usage and atomic reservation schema with the platform friend,
  who coordinates migration files. Do not independently allocate migration numbers.
- [ ] After owner policy approval, implement the approved provider with explicit model/location,
  bounded retries/timeouts and no silent region/provider/mock fallback.
- [ ] Persist tenant-scoped retrieval and usage/cap state. Test restart recovery,
  cross-tenant denial, concurrent reservations and kill-switch behavior.
- [ ] Give the platform friend the tested extraction contract and safe failure categories.

## 3. Dependencies and limits

The provider proposal is not activation approval. Provider, processing location, effective
retention, budget and account/capacity remain unresolved. Recheck dated provider details
before activation. No paid calls or external invoice transfers until explicitly approved.

Already selected: minimized text/bounded images under proposed controls, one non-login
automation maker per firm, and statement exclusion for v1. Serving India/EU does not
authorize cross-border routing. Extraction never grants posting/payment authority.

## 4. Done and delivery

Done requires durable state across restarts, tenant isolation and concurrent cap enforcement,
plus approved synthetic live-provider evidence. Mock evals alone are insufficient.
Each PR: scope, exact commands/counts, failure cases, blockers and hosted changes (or none).
Require green CI on the exact commit. Update relevant docs/HANDOVER with factual results.

Never share/commit `.env`, keys, invoice contents or tokens. Do not edit `person4_tally/`,
weaken RLS, force-push main or provision paid resources. No AI attribution trailers.
Coordinate API/schema changes before editing callers; keep PRs small.
