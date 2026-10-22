# FinLedger handover (shared by ChatGPT/Codex and Claude)

This file is the single baton between AI agents working on FinLedger. **Read all of it before touching code.**
Whoever is working updates the "Latest status" section and pushes it before stopping. It replaces the old
`HANDOVER_CLAUDE.md`.

---

## 🔁 Hand-back protocol (MANDATORY, read first)

When your credits/usage are running low (**owner's updated threshold: start at ~5% remaining, not at 0%**):

1. **Stop starting new work.** Finish or cleanly park the current change.
2. **Commit everything.** Nothing may stay uncommitted on the Windows machine. The last handover lost
   migration 008 and the JWT verifier this way and they had to be recovered from Supabase and a WIP branch.
   - Finished and tested work: branch `handover/<short-topic>`, push, open a PR.
   - Unfinished work: commit anyway on `wip/<short-topic>` with a commit message that starts with `WIP:` and push it.
     Failing tests are OK on a `wip/` branch if the commit message lists them.
3. **Update the "Latest status" section of this file** (in the same push): what you did, what's verified (exact
   test counts / commands), what's half-done, which branch it's on, anything you applied to Supabase staging.
4. **Never apply SQL to Supabase staging without also committing the same SQL file** in
   `person2_platform/src/finledger_platform/migrations/` in the same session.
5. Tell the user, in one message: "Handing back to Claude. Read HANDOVER.md on branch `<branch>`." Then stop.

Claude does the same in reverse when handing to you.

---

## Mission and hard rules

- Product: a sellable multi-tenant accounts-payable app for Indian CA firms. Persons 1–3 only.
  **Never edit `person4_tally/`.**
- Never print, commit or paste secrets. Real values live only in environment/secret settings. `.env` is gitignored.
- Never run the app as `postgres`/service role/any RLS-bypassing role. App role = login that is a member of
  `finledger_app` (the code refuses others via `assert_rls_applies`).
- Never claim "production ready" while open items below remain.
- Be explicit about what is live, local-only, untested, or blocked.
- **Owner rule (hard): no AI attribution in Git.** Never add `Co-authored-by`, `Claude-Session`, "Generated with …"
  lines, or an AI author identity (e.g. `Claude <noreply@anthropic.com>`) to commits, PR titles/bodies or comments.
  Commit as the owner (`git config user.name ryanshaon`,
  `git config user.email 250547248+ryanshaon@users.noreply.github.com`). Check `git log -1 --format='%an <%ae>%n%b'`
  before every push.
  GitHub squash defaults can change the author email and append owner co-author trailers. For future merges,
  pass `--author-email 250547248+ryanshaon@users.noreply.github.com` and a reviewed `--body-file` explicitly,
  then inspect the published commit as well. Never silently rewrite shared history to repair metadata.
  The GitHub squash endpoint rejected that noreply override on PR #20. A safe alternative used for PRs
  #20/#21 is a **non-forced fast-forward** of the exact reviewed branch after all PR checks pass; this
  preserves the already-verified commit identity/message. Abort if it is not a fast-forward. GitHub
  subsequently marks the PR merged. Never use a force push as a routine PR integration method.

## Repository

- **Three-contributor onboarding:** [TEAM_WORK_SPLIT.md](docs/TEAM_WORK_SPLIT.md) assigns AI,
  platform/deployment and controls/QA ownership, dependencies and acceptance checks. Use it for friends
  joining the project; this handover remains authoritative for decisions and hosted state.

- GitHub: `ryanshaon/FinLedger`. Default branch `main` (protected by habit: changes go via PR, CI must be green).
- Windows checkout: `C:\Users\ryans\OneDrive\Documents\Studies\Projects\FinTechShi\FinLedger_GitHub`.
- Layout: `packages/`, `evals/`, `person1_ai_rag/` (Person 1 AI/RAG) · `person2_platform/` (API, tenancy, DB,
  workers, storage, auth) · `person3_control_ui/` (controls, workflow, review UI) · `person4_tally/` (out of scope).

## How to verify (same commands as CI, `.github/workflows/ci.yml`)

```bash
uv sync --project person2_platform --python 3.12 --locked --all-extras --dev
uv sync --project person3_control_ui --python 3.12 --locked --dev
PYTHONPATH=evals person2_platform/.venv/bin/python -m pytest packages evals -q
PYTHONPATH=evals person2_platform/.venv/bin/python evals/run_evals.py --provider-mode mock
PYTHONPATH=person2_platform/src person2_platform/.venv/bin/python -m pytest person2_platform/tests -q
PYTHONPATH=person2_platform/src:person3_control_ui/src person3_control_ui/.venv/bin/python -m pytest person3_control_ui/tests -q
docker build -t finledger:ci .        # CI "image" job
```

Verified baseline through PR #30: Person 1 **43 passed**, evals **3/3 + 5/5**, Person 2 **333 passed**,
Person 3 **55 passed**; `test` + `image` CI green on `731f9e9`. Re-run exact counts after code changes.
- Cloud containers: PostgreSQL may be installed but not on PATH. Use
  `export PATH=/usr/lib/postgresql/16/bin:$PATH`.

- DB tests start a throwaway PostgreSQL via `initdb` (needs PostgreSQL on PATH). As root they run it as the
  `postgres` OS user automatically. On Windows the sandbox used to stall `initdb`: prefer CI or WSL.
- `run_evals.py` rewrites the timestamp in `evals/latest_report.md`; avoid running it just for a handover. If you
  run it, either include the updated report intentionally or restore only that generated file after checking the
  diff. Do not discard unrelated work.
- If you change `person2_platform/pyproject.toml` dependencies, run `uv lock` in **both** `person2_platform` and
  `person3_control_ui` (Person 3 depends on Person 2), then `uv lock --check` both.

## Supabase staging (project `hxymklwqifwojziewtcv`, region `ap-south-1`, no customer data)

State verified 2026-10-09:
- Both Supabase's migration history and `public.schema_migrations` include `001`–`011`. Migrations 009, 010 and
  011 were applied from the exact committed files after green PRs #7, #9 and #10. **Do not re-apply them.**
- `finledger_private.staff_sessions` and `staff_session_events` have RLS enabled and no direct SELECT grants to
  `anon`, `authenticated` or `finledger_app`; all have zero rows. Security advisor only reports intentional INFO
  “RLS enabled, no policy” for these two tables and `schema_migrations`.
- 24 public tables, all RLS on; `anon`/`authenticated`/`service_role` have no table grants (006).
- Bucket `finledger-documents-dev`: private, 25 MiB limit, 0 policies (intentional: app uses S3 keys), 0 objects.
- Firms/clients/vendors: 0 rows.
- Hosted RLS proof (rollback-only) passed under `finledger_app`:
  - no tenant context → 0 rows
  - client A sees only itself
  - A cannot insert into B
  - firm 2 sees only its own client
  - Cross-tenant UPDATE/DELETE were proven in an earlier run and locally; via the Supabase MCP connector,
    statements containing UPDATE/DELETE hang until timeout (likely a hidden approval gate), so run those
    through `finledger-platform isolation-check` with the owner DSN instead.

Gotchas:
- On Supabase, `postgres` is an ADMIN member of `finledger_app` but **without SET**. To `set role finledger_app`
  in a test transaction you must `grant finledger_app to postgres with set true` inside that same transaction
  (rolled back with it). `isolation-check` already does this.
- No app login role (e.g. `fl_app`) exists on staging yet; none has been created with a real password.

## What exists (all on `main`)

| Area | Where | State |
|---|---|---|
| Tenancy + RLS, queue, intake, storage, outbox worker | `person2_platform` | tested |
| Supabase hardening 006/007 | migrations | applied on staging |
| Auth subject link 008 | `migrations/008_supabase_auth_subject.sql` | applied on staging. Rule: **never auto-link by email/JWT metadata**; admin links explicitly |
| Supabase JWT verifier + Auth/session backend | `finledger_platform/supabase_auth.py`, `auth_gateway.py`, `browser_sessions.py` | tested offline and with local Postgres; PRs #7–#9 merged. Browser routes are on `main`, not deployed to staging |
| Staging proof tools | `finledger-platform isolation-check`, `finledger-platform s3-smoke` | tested; s3-smoke never run against real Supabase (no valid keys yet) |
| Runtime image + local stack | `Dockerfile`, `compose.yaml` | image includes installed `finledger-ai` (PR #17); image CI verifies imports, resources and hosted mock guards. No extract consumer |
| Control UI entry point | `finledger-control` | uses `make_pool` (RLS guard) and full S3 settings |
| Staff invites + first login | `finledger_platform/staff_invites.py`, Person 3 `/admin/staff`, `/auth/accept` | merged in PR #15; tested offline with fake Auth only. Disabled until `SUPABASE_SECRET_KEY` is set |
| Worker diagnostic privacy | `worker.py`, `outbox_worker.py`, `tests/test_worker_diagnostics.py` | PR #18: fixed failure categories, no exception payload in worker-owned logs or standard escaping tracebacks; retry/rollback/lease recovery tested |
| Auth design | `docs/plans/AUTH_SUPABASE_DESIGN.md` | implemented except staging Auth settings, hosting and real-user E2E |

## ✅ Owner checklist (things only the owner can do)

Tick these off in order. An agent can help explain each step but must never see or type the secret values.

1. **Windows checkout aligned (completed 2026-10-09).** This checkout now follows rewritten `origin/main`;
   the pre-rewrite history is preserved locally at `archive/pre-history-rewrite-20261009` and was not pushed.
   Do not reset again just to repeat this checklist. For any other older clone, preserve local work before
   aligning it with rewritten `origin/main`.
2. **Optional cleanup on GitHub:** delete merged branches `claude/cloud-session-h52oob`, `claude/lucid-faraday-oruuq9`
   and `handover/*`. They still hold pre-rewrite commits.
3. **Windows `.env` for S3:** copy `.env.example` only if `.env` does not exist, then open `.env` yourself;
   never overwrite existing credentials with the example. Set `FINLEDGER_STORE=s3`,
   `FINLEDGER_S3_BUCKET=finledger-documents-dev`,
   `FINLEDGER_S3_ENDPOINT_URL=https://hxymklwqifwojziewtcv.supabase.co/storage/v1/s3`,
   `FINLEDGER_S3_REGION=ap-south-1`, `FINLEDGER_S3_ADDRESSING_STYLE=path`, and **type** `AWS_ACCESS_KEY_ID` /
   `AWS_SECRET_ACCESS_KEY` yourself. `.env` is gitignored; never paste the keys into chat. Then run
   `finledger-platform s3-smoke` (open item 1).
4. **Supabase invite setup** (before any real invite), from `AUTH_SUPABASE_DESIGN.md` §3.4:
   - put `SUPABASE_SECRET_KEY` only in the control UI server's secret settings;
   - Supabase → Auth → Email templates → *Invite user*: link =
     `{{ .SiteURL }}/auth/accept?token_hash={{ .TokenHash }}&type=invite`;
   - set Site URL + redirect allow-list to the control UI origin; turn public sign-ups **off**;
   - access-token expiry 900 s; enable TOTP MFA; leaked-password protection on.
5. **Approve the remaining AI provider/data policy** (see [ADR-001](docs/decisions/ADR-001-AI_PROVIDER.md)):
   - a. **Decided 2026-10-10:** owner delegated the better choice; use one non-login "FinLedger automation"
     maker per firm, with no Auth subject, token or firm-admin privilege. Not yet implemented.
   - b. **Pending approval:** provider, processing jurisdiction, permitted invoice text/images, retention/logging
     exceptions, budget and capacity. Owner requested research, not provider activation. ADR-001 is proposed.
     **Follow-up:** minimized invoice text and bounded page images are permitted under the proposed controls;
     serve Indian and EU markets, but decide processing locations later. Budget remains undecided. Do not
     infer either cross-border permission, country-local routing, a capacity purchase or live-model activation.
   - c. **Decided 2026-10-10:** owner delegated the better choice; exclude bank/account statements from v1
     extraction and handle them as an explicit exception. Not yet implemented in the consumer.
6. **Choose a password for the staging app login role** (open item 3) and store it only in secret settings.
7. **Email:** verify a sender domain in Resend and store the API key in secret settings (open item 4).
8. **Hosting:** approve an account and budget (open item 5).

## Open work, in priority order

| # | Item | State | Blocked on |
|---|---|---|---|
| 1 | **S3 smoke test against Supabase** (`FINLEDGER_STORE=s3 … finledger-platform s3-smoke`) | code ready, never run live | owner checklist 3 |
| 2 | **Sign-in staging E2E** (Playwright: invite → set password → login → TOTP → client list) | all code merged (PRs #7–#9, #15); migrations 009–011 applied, **never re-apply** | owner checklist 4 + hosting |
| 3 | **App login role on staging** (`fl_app`-style, member of `finledger_app` + worker roles, via the Supabase **session pooler**) | not created | owner checklist 6 |
| 4 | **Email delivery** (Resend outbox implemented; inbound webhook untested) | code ready | owner checklist 7 |
| 5 | **Hosting** (recommendation: AWS ECS/Fargate Mumbai next to Supabase `ap-south-1`; Render is the simple alternative) | image ready | owner checklist 8 |
| 6 | **Person 1 release blockers:** live provider, extract queue consumer, persistent RAG/usage/cap state, PII policy | plan in the audit (documentation only) | owner checklist 5 (except 6a below) |
| 6a | **Package Person 1 into the runtime image** | implemented and verified in PR #17 | none; no provider or consumer added |
| 6b | **Worker diagnostic sanitization** | implemented and verified in PR #18 | none; no hosted changes |
| 6c | **Malformed control form inputs** | PR #21 integrated with green CI; four regression cases | none; no hosted changes |
| 6d | **Browser cookie MFA assurance binding** | PR #22 integrated after independent review and green CI | none; no migration or hosted changes |
| 6e | **Hosted antivirus configuration guard** | PR #25 integrated after green CI | none; no migration or hosted changes |
| 6f | **Credential URL access-log privacy** | PR #26 integrated after green CI | none; proxy logging remains operator configuration |
| 6g | **Signed-download validation and safe filenames** | PR #27 integrated after independent review and green CI | none; local-store boundary only |
| 6h | **Storage smoke failure/report accuracy** | PR #27 integrated after independent review and green CI | no keys needed for code tests; live smoke still blocked |
| 6i | **Strict bounded scanner response validation** | PR #28 integrated after independent review and green CI | none; no migration or hosted changes |
| 6j | **Email webhook request boundary** | PR #29 integrated after independent review and green CI | none; live email remains blocked |
| 6k | **Aggregate scanner deadline and post-auth email DB acquisition** | PR #30 integrated after independent reviews and green CI | none; no hosted changes |
| 7 | **Performance:** 28 unindexed FKs, 13 "unused" indexes on an empty DB | do not act on zero-data advisor stats | real query plans |

Owner decisions already answered (do not re-ask): password + TOTP MFA; SSO deferred; 8 h absolute / 30 min idle
browser sessions, applied to all staff with AAL2 required before client data.
Also decided: per-firm non-login automation maker and statement exclusion for v1. These two choices do not
approve a live provider, invoice transfer, paid resources or automatic posting/payment.

## ▶️ Next agent task (resume the first owner-unblocked item)

**Packaging (PR #17), worker diagnostic sanitization (PR #18), form input hardening (PR #21) and browser MFA
cookie assurance (PR #22), scanner configuration/logging guards (PRs #25/#26), signed downloads/storage
proof reports (PR #27), strict scanner protocol validation (PR #28), email request limits (PR #29),
and scanner deadlines/post-auth email pool acquisition (PR #30)
are implemented and verified. Do not repeat them.** The remaining Open work items need credentials, configuration, an approved hosting account/budget,
owner policy decisions, or real query plans. Start from current `main` and inspect the linked PRs before editing.

**Metadata correction completed with explicit owner approval:** PR #19 had green `test` and `image` checks
on `f7cc5ac`. An exact-lease push replaced the expected `03ec760` remote tip with that reviewed tip.
Replacement commits `d77b124` and `12c93c1` preserve the original application trees and dates, with the
owner's noreply identity and no attribution trailers. GitHub automatically marked PR #19 merged.
The original tip remains on local `archive/pre-owner-metadata-correction-20261009`; do not push it.
Other clones must preserve local work before aligning with the updated history. Do not repeat this rewrite.

1. If the owner has entered real S3 keys privately, run Open work 1 with the private staging bucket. Do not print
   environment contents or secret values. Record actual smoke-test results and cleanup; never claim it was run
   from a configuration-presence check.
2. If the Auth settings, app login role, private server secrets and runtime are ready, run the real staging
   invite → password set → login → TOTP → client-list E2E. Continue to use the non-owner app role and explicit
   Auth-subject binding. Migrations 009–011 are already applied; do not re-apply them.
3. Maker and statement choices are recorded in checklist 5. Once its remaining provider/processing/data-policy
   and spend gates are explicitly approved, implement the adapter/accounting and extract consumer from the
   audit/ADR design and test lists on a new branch. Research authorization is not activation approval.
4. If none is unblocked, report the exact missing inputs rather than guessing policies, provisioning paid
   resources or claiming production readiness. The provider proposal is researched; ask only the remaining
   ADR approval questions, not the already-decided maker/statement questions.

Keep the owner's **5% remaining** usage hand-back threshold, update Latest status before stopping, and push
every completed or WIP change. No uncommitted implementation may remain on the PC.

---

## Latest status

**2026-10-22 platform integration (Harshith, branch `feat/platform-integration`):** Added migration
`012_ai_durable_state.sql` (not applied anywhere hosted): `rag_chunks` unique `(client_id, ref)` with
`legacy:`/`auto:` refs for empty values and a hard stop on existing duplicates, `llm_usage_rows`,
`llm_token_reservations`, tenant RLS, `llm_reserve/reconcile/release_tokens` (signatures match
`PostgresKillSwitch`, extract/score/map worker roles only) and one non-login automation maker per firm
(`users.automation`; no token, Auth subject, firm-admin flag or client roles). Added
`finledger-platform extract-worker` plus a Compose service (mock provider, local only): statements and
keys outside `{client_id}/` become document exceptions before storage/AI; success settles `extracted`,
a maker-attributed workflow event and a `score` job in one transaction that re-checks the lease.
Local run: **71 AI / 355 platform / 55 UI passed**, mock **3/3 + 6/6**, 0 skipped. Open for Ruthvik:
`score` job consumer, wiring `PostgresKillSwitch` (reserve/reconcile inside short `tenant()` scopes)
and retiring the `ai_durable_ephemeral.sql` overlay, which redefines the 012 functions in AI tests.
Hosted login/grants, storage/email/scanner proofs and deployment remain blocked on owner approval.

**2026-10-22 consolidated release assignment:** Synced main to `cf5d2f0` (PR #37). Added
`docs/assignments/RELEASE_INTEGRATION.md` for remaining AI runtime integration, platform integration,
staging operations and controls/QA, including work previously assigned to Ryan. Existing friend's
accepted migration/consumer handover stays coordinated rather than duplicated. Ryan retains account,
policy/budget approvals, final independent acceptance and launch authority. TEAM_WORK_SPLIT links the
updated assignment. Branch `docs/release-integration-assignment`; documentation only, no hosted changes,
secrets, runtime edits or Person 4 edits. No local suite rerun. PR #37 CI evidence inspected this session:
**71 AI / 333 platform / 55 UI passed**, mock **3/3 + 6/6** (run `39399420351`, October 20).
P1 Gemini fake-transport and durable Postgres adapters now exist; older statements below saying no
adapters exist are historical. Live transport/activation, runtime wiring and production migrations remain
unfinished. New brief requires local link/whitespace validation and green exact-commit PR CI before merge.

**2026-10-17 individual assignment files:** Added three shareable Markdown briefs under
`docs/assignments/`, linked from TEAM_WORK_SPLIT. Friend 1 owns AI, Friend 2 platform;
Ryan is assigned controls/QA as the easiest starting role, without relaxing security review
or owner-only policy/account responsibilities. Branch `docs/individual-team-assignments`.
Documentation only; no local application suite rerun or new test counts claimed. Historical
baseline remains **43 AI / 333 platform / 55 UI**, mock **3/3 + 5/5**. No hosted changes,
credentials, model/email calls, runtime edits or Person 4 edits. Validate links/whitespace
and require exact-commit green CI before integration. PR #35 is merged; no WIP implementation.

**2026-10-17 three-person handoff:** Synced clean main to `346d98a`, including independently
contributed Person 4 files; no edits or readiness assessment of that pack. Added
`docs/TEAM_WORK_SPLIT.md` with three scoped assignments, shared-file ownership, owner gates,
acceptance checks and merge order. Linked it from README and this file; corrected root README's
outdated claim that Person 4 contains architecture only. Cleanup is documentation/navigation only:
no files, volumes, backup branches or private configuration deleted. Branch `docs/three-person-handoff`.
No runtime edits, local suite rerun, hosted SQL/configuration, email or model calls. Historical baseline
remains **43 AI / 333 platform / 55 UI**, mock **3/3 + 5/5**; exact PR CI results must be checked before
integration. PR #33's provider proposal is already merged on main. No unfinished implementation added.
Next: friends select A/B/C roles and complete the first local deliverables while owner gates stay closed.

**2026-10-10 owner decisions and provider research:** Owner delegated maker and statement choices and
asked to research AI providers. Selected per-firm non-login automation maker and statement exclusion for
v1; neither is implemented yet. Added proposed ADR-001 comparing official Google Cloud, Bedrock and
OpenAI documentation, with jurisdiction/retention/lifecycle/capacity caveats and an illustrative cost model.
Conditional recommendation: Gemini 3.5 Flash, India processing only if its provisioned-capacity quote is
approved; EU PayGo is a separate cross-border option requiring explicit approval. No provider/data-transfer
or spending approval is inferred beyond the bounded input choice: owner subsequently permitted minimized
text and bounded page images under proposed controls, selected serving both markets with processing
locations decided later, and deferred budget selection. Location, provider activation, account retention and
exception/spend gates remain unresolved. Do not re-ask maker/statement/input-modality questions.
Branch `docs/ai-provider-options` (PR #33); documentation only, no local test rerun
or new test counts claimed. Runtime baseline remains **333 platform / 55 UI / 43 AI**, mock **3/3 + 5/5**.
No credentials, hosted configuration/SQL, migrations, identities, customer data, email, model calls or Person 4
changes. Next: finalize jurisdiction/routing, verify effective retention and resolve exceptions/budget; then
verify account/capacity before adapter/accounting/consumer implementation. Do not repeat maker/statement
questions. Provider proposal and metadata were checked against official sources; no benchmark or legal
compliance guarantee is claimed. All edits must be committed/pushed through green PR CI before integration.

**2026-10-10 readiness recheck:** Refreshed clean `main` at `43fbe2f` (PR #31 status update merged).
Read this handover completely. Presence-only checks still report S3 access/secret keys, Supabase
publishable/secret keys and Resend key as missing/placeholders; no secret values were displayed.
Owner checklist 5's maker, AI provider/region/data-retention policy and statement choices remain unanswered;
hosting approval, Auth setup and app-login password remain owner gates. No newly unblocked Open work item.
No application changes, fresh test counts, hosted calls/SQL, migrations, identities, email or Person 4 edits.
The verified **333 platform / 55 UI / 43 AI** counts below are from the previous completed batch, not rerun
today. Resume the first owner-unblocked item; do not repeat completed hardening or consume credits simply
to reach the 5% threshold. Today's status branch is `handover/owner-gates-20261010`.

**2026-10-09 — local production-boundary hardening; no deployment performed.**

- Main includes PRs #24–#30 with green `test` and `image` CI. Current published application tip is
  `731f9e9`; final status-only branch is `handover/resource-safety-status`. Integration used non-forced
  fast-forwards of reviewed commits with the owner's noreply identity,
  without attribution trailers. No additional history rewrite was performed. The historical metadata
  correction and local backup branches are documented above; do not repeat or push those backups.
- PR #24 adds [DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md), derived from actual CLI/runtime code.
  README guards existing `.env` files against overwrite. Locked offline project reinstalls repaired stale
  local console launchers; both `.exe --help` checks passed. No dependencies were upgraded.
- PR #25 rejects unknown scanners in every environment and EICAR-only scanning in staging/production.
  PR #26 disables Uvicorn access logs at both CLI launch boundaries to avoid credential-bearing URL logs.
  Operator-managed proxy/custom-server logging still requires private configuration.
- PR #27 rejects malformed signed credentials/claims before object reads, validates finite expiry and safe
  filenames, and encodes Unicode download filenames safely. S3 smoke uses fixed safe failure labels,
  preserves cleanup results after errors, and counts only 401/403/404 as unsigned denial. Other errors,
  including legacy 400, remain unverified/fail-closed. No real S3 proof has been run. Both changes received
  independent review without remaining actionable findings.
- PR #28 requires complete bounded ClamAV replies and exact clean-result framing. Eight regression cases
  failed before implementation; independent offline review found no actionable issue. This does not add
  a whole-operation deadline; the follow-up branch below addresses the socket-I/O budget.
- Fresh complete suites through PR #28: Person 2 **274 passed**, Person 3 **55 passed**, Person 1 **43 passed**;
  mock evaluations **3/3 structural goldens + 5/5 exact matches**. Existing Starlette deprecation warning
  remains. CI supplies Docker image verification; Docker is unavailable locally.
- PR #29 email request-boundary implementation: incremental bounded
  buffering stops at the first oversized chunk; malformed/non-ASCII HMAC headers return 401 before comparison.
  Synthetic ASGI tests: **18 failed / 5 passed** before the fix, **23 passed** afterward. Parent full-platform
  verification passed **297 platform tests**; UI **55 passed**, AI **43 passed**, mock evals **3/3 + 5/5**.
  Independent review found no remaining actionable issue; all PR test/image checks passed before integration.
  Verification commands used the suites above with workspace `--basetemp=.pytest-inbound-platform`,
  `.pytest-inbound-control`, `.pytest-inbound-ai`, and mock report `.pytest-inbound-ai/eval_report.md`.
  Accepted bodies remain buffered for HMAC/MIME
  parsing, with a brief bounded byte-copy; proxy/server limits still matter.
- **Supabase unchanged:** no SQL, migrations, Auth users/invites, hosted settings, provider calls or emails.
  No secret values were read/printed; no Person 4 edits. Presence-only private configuration check still
  found S3, Supabase and Resend keys missing/placeholders. Owner maker/provider-PII/statements questions
  remain unanswered, as do deployment inputs in the Owner checklist. Never guess those decisions.
- PR #30, `hardening/runtime-resource-boundaries`: aggregate scanner socket-I/O deadline has
  **17 failed / 1 passed** regression cases before implementation and **18 passed** afterward; first full
  platform verification **315 passed**. System DNS may overrun connection timeout, but late completion is
  rejected. A synthetic ASGI review reproduced unauthenticated email uploads holding DB connections
  before HMAC validation; the post-auth/threadpool fix has **14 expected red failures**, then **37 focused
  tests passed** with real ASGI dependency ordering and worker threads. Independent review of both fixes
  found no remaining actionable issue. Scanner protocol tests now require a complete INSTREAM request,
  actual reply transmission and propagated server-thread failures. Three harness regression tests failed
  before the fix; **30 scanner/deadline focused tests passed** afterward on real local loopback, without
  thread warnings. This prevents a network-denied run from falsely passing on connection failure.
  Final complete suites: **333 platform / 55 UI / 43 AI passed**, mock evaluations **3/3 + 5/5**,
  with only the existing Starlette warning. Commands used the suites above with workspace basetemps
  `.pytest-resource-final-platform`, `.pytest-resource-final-control`, `.pytest-resource-final-ai`, and
  mock report `.pytest-resource-final-ai/eval_report.md`. All implementation is finished and reviewed;
  both push/PR CI `test` and `image` checks passed on exact commit `731f9e9` before main integration.
  No unfinished implementation or WIP branch. Do not repeat these completed fixes.
- Keep the owner's **5% remaining** hand-back rule. Current usage was above that threshold (28% primary /
  53% weekly remaining at the last check); do not repeat completed tests simply to spend credits.
  Resume the first owner-unblocked item, using the Owner checklist and Next agent task above. Do not guess
  decisions or provision paid resources. PR #31 published the status-only update with green CI and is on
  main; no unfinished implementation remains. No hosted proof or production readiness is claimed.
