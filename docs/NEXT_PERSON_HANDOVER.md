# FinLedger Friend 1 — handover for the next person

**Audience:** Ruthvik → next contributor picking up AI (`packages/`, `evals/`).  
**Date:** 2026-10-20 (UTC).  
**Repo:** [FinLedger](https://github.com/jastiruthvik-web/FinLedger) (this workspace checkout).

---

## What already landed

PRs [#1](https://github.com/jastiruthvik-web/FinLedger/pull/1), [#2](https://github.com/jastiruthvik-web/FinLedger/pull/2), and [#3](https://github.com/jastiruthvik-web/FinLedger/pull/3) are **merged to `main`** on 2026-10-20.

| PR | Branch | What it contains |
|---|---|---|
| [#1](https://github.com/jastiruthvik-web/FinLedger/pull/1) | `docs/ai-durable-schema` | **Proposal only** — `docs/proposals/AI_DURABLE_RETRIEVAL_USAGE_SCHEMA.md` for `rag_chunks` upsert key, `llm_usage_rows`, `llm_token_reservations`, and `llm_*_tokens` functions. Friend 2 still authors the migration (next after `011`). |
| [#2](https://github.com/jastiruthvik-web/FinLedger/pull/2) | `feat/ai-fixtures` | Synthetic invoice fixtures, fail-closed regressions (malformed output, bounded images, minimized inputs, invoice-free diagnostics), `credit_note` fixture, stable `ExtractFailure` categories for extract/vision. |
| [#3](https://github.com/jastiruthvik-web/FinLedger/pull/3) | `feat/ai-durable-provider` | Gemini adapter behind `complete(...)` with **fake transport only** in tests; `ModelRouter` supports `mock` and `gemini` (gemini requires explicit transport + `FINLEDGER_GEMINI_LOCATION`); **both `mock` and `gemini` blocked in `staging`/`production`** until owner activation. Durable Python adapters (`PostgresIndexer`, `PostgresKillSwitch`, `PostgresUsageWriter`) proven on **ephemeral Postgres** with overlay SQL in `packages/testing/sql/ai_durable_ephemeral.sql` (not a platform migration). `document_class=statement` → `statement_not_extracted` (no provider call). `extract/contract.py` for Friend 2. Opt-in `--live-synthetic-eval` gate in `evals/run_evals.py` (off in CI). |

**Not done (owner gate):** live provider calls, paid API usage, external invoice transfer, staging proof under a runtime login, or applying durable SQL to Supabase.

---

## Owner gate (hard stop)

Per `HANDOVER.md` checklist **5b** and **ADR-001** (proposed): **no** activation until the owner explicitly approves:

- Provider and model  
- Processing jurisdiction / endpoint  
- Effective retention (including logging exceptions)  
- Budget and account/capacity  

Research and minimized-text/bounded-image decisions **do not** authorize live calls. `FINLEDGER_LLM_PROVIDER=gemini` in production/staging **raises** until policy and hosting are approved.

---

## What Friend 2 (platform) must do next

1. **Migration** — Assign the next number after `011`. Implement the proposal in `docs/proposals/AI_DURABLE_RETRIEVAL_USAGE_SCHEMA.md` under `person2_platform/src/finledger_platform/migrations/` (unique `(client_id, ref)` on `rag_chunks`, `llm_usage_rows`, `llm_token_reservations`, RLS, grants to `finledger_worker_extract` / other model workers as needed, `llm_reserve_tokens` / `llm_reconcile_tokens` / `llm_release_tokens`). Friend 1’s overlay SQL in `packages/testing/sql/` is the behavioral reference for tests only.
2. **`finledger-platform extract-worker`** — Claim `extract` jobs, `tenant(conn, client_id)`, call Friend 1 `extract_document(...)` / dispatcher with `load_extract_input`, map failures to queue `last_error` categories (see `packages/extract/failures.py` and `extract/contract.py`).
3. **Statement exception** — For `document_class = statement`, set document `exception` and finish without extract (Friend 1 already raises `statement_not_extracted` if called).
4. **Object-key guard** — Reject S3 keys not under `{client_id}/` before calling Person 1 (`object_key_rejected`).
5. **RLS + runtime login** — Staging proof under `finledger_app` (e.g. `fl_app`), not owner/service role. Migrations `001`–`011` on staging are **already applied**; do not re-apply.

---

## What Ryan (controls / QA) must do

- Review **maker/checker** separation for the per-firm automation maker (Friend 2’s row; no Person 1 `actor_id`).
- Review UI handling for **statement** uploads and extract failure categories shown to staff (no invoice body in errors).
- Lead **upload → review** proof once extract consumer and hosting gates clear; Person 1 fixes extraction contract bugs that proof finds.
- Keep release checklist and Auth proof on owner gates (`HANDOVER.md`); do not weaken review to demo a model.

---

## How to verify (Person 1)

From repo root (Python 3.12, `uv`):

```bash
export PATH=/usr/lib/postgresql/16/bin:$PATH   # if initdb not on PATH
uv sync --project person2_platform --python 3.12 --locked --all-extras --dev
PYTHONPATH=evals person2_platform/.venv/bin/python -m pytest packages evals -q --basetemp=.pytest-person1
PYTHONPATH=evals person2_platform/.venv/bin/python evals/run_evals.py --provider-mode mock --report .pytest-person1/eval_report.md
```

Durable integration tests: `packages/usage/tests/test_durable_postgres.py` (needs `initdb` or `FINLEDGER_TEST_OWNER_DSN`).

Mock evals remain the CI contract check (`3/3` structural + `6/6` extraction). `--live-synthetic-eval` is **opt-in** and must not run in CI without owner approval.

---

## Key files (Person 1)

| Area | Path |
|---|---|
| Extraction contract | `packages/extract/contract.py`, `packages/extract/failures.py` |
| Statement / vision | `packages/extract/vision_fallback.py` |
| Gemini adapter | `packages/usage/providers/` |
| Router | `packages/usage/model_router.py` |
| Durable adapters | `packages/rag/postgres_indexer.py`, `packages/usage/postgres_kill_switch.py`, `packages/usage/postgres_usage_writer.py` |
| Ephemeral SQL (tests only) | `packages/testing/sql/ai_durable_ephemeral.sql` |
| Live eval gate | `evals/run_evals.py` (`--live-synthetic-eval`) |

---

## Hosted changes

**None** from Friend 1 durable/provider work. Supabase stays at migrations `001`–`011` with no customer data until Friend 2 and the owner apply the next steps.
