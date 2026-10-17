# Ryan — Controls, UI and testing

## Objective: your assignment

**This is the easiest starting role of the three.** Start with the existing UI/tests,
record bugs and make small usability fixes. You do not need to build the provider,
database persistence or deployment infrastructure. Security checks still need experienced review.

Repository: https://github.com/ryanshaon/FinLedger

## 1. Start here

Read root `HANDOVER.md` completely, `docs/TEAM_WORK_SPLIT.md`, the Person 3 architecture
and `docs/plans/AUTH_SUPABASE_DESIGN.md`. Use Python 3.12, uv and PostgreSQL tools.
From your existing repo root:

```powershell
git switch main
git pull --ff-only
git switch -c test/control-review-checklist
powershell -ExecutionPolicy Bypass -File scripts/test.ps1
```

Record fresh results. The historical baseline is 43 AI, 333 platform and 55 UI tests,
plus 3/3 and 5/5 mock checks; do not report those as a new run.

## 2. First deliverable — no live keys needed

- [ ] Run the existing tests and note actual pass/fail/skip counts.
- [ ] Write a checklist using synthetic invoices: review, correction, rejection,
  duplicate submission, invalid form input and another client's invoice access.
- [ ] For every issue, record steps, expected result, actual result and a redacted screenshot.
- [ ] Add regression cases using existing fixtures/fake Auth; fix small copy/validation-display
  issues in `person3_control_ui/` without changing security rules.
- [ ] Give backend failures to Friend 2 and AI failures to Friend 1. Do not duplicate their work.

The local stack does not yet provide a full invoice demo. Browser Auth needs configuration,
and automatic extraction awaits the missing worker. A blocked test is not a passing test.

## 3. After your friends deliver the dependencies

- [ ] Ask Friend 2 for the approved staging URL and synthetic-staff testing setup.
- [ ] Test invite → set password → login → TOTP → client list → logout.
- [ ] Check expired/revoked sessions and cross-client denial. Access before completing MFA
  must fail; preserve 8-hour absolute / 30-minute idle session limits and deferred SSO.
- [ ] With both friends, test synthetic upload → extraction → score/map → human review,
  including statement exclusion, scanner outage, provider timeout, budget exhaustion and retries.
- [ ] Review automation-maker behavior with Friend 2: no login/token/admin privilege,
  and no path for a human editor to approve their own work. Get experienced security review.
- [ ] Check that pending/failed jobs are not presented as successfully processed invoices.

## 4. Done and delivery

Deliver a reproducible checklist, bug fixes with regression tests, and redacted evidence
listing commit, environment, commands, counts, blockers and cleanup. Require green CI before
integration. Publish small PRs and update HANDOVER; do not call the product production-ready.

Owner responsibilities are separate from this easier implementation role: you still approve
hosting/AI budgets and policy, and enter private account credentials/settings yourself.
Do not paste/share `.env` or secrets. No real customer invoices, paid calls, real invites,
outbound email or production changes without the appropriate approval.
Never bypass MFA/RLS/scanning, force-push main or edit `person4_tally/`.
