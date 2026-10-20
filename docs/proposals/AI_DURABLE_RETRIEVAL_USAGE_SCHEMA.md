# Proposal: durable retrieval, usage, and token reservations (Friend 1 → Friend 2)

**Status:** Proposal only — Friend 2 assigns the next migration number after `011`, authors the SQL file under `person2_platform/src/finledger_platform/migrations/`, and applies on staging after review. Friend 1 does not edit applied migrations `001`–`011` or pick a number here.

**Audience:** Friend 2 (platform), Ryan (controls), Friend 1 (Python adapters in `packages/` after merge).

**Goal:** Replace in-memory `Indexer`, `UsageWriter`, and `KillSwitch` with tenant-scoped Postgres state that survives restarts and enforces per-bill token caps across concurrent workers.

---

## 1. Scope and non-goals

| In scope | Out of scope |
|---|---|
| Tenant-isolated RAG chunk persistence (align with existing `rag_chunks`) | pgvector dimension, HNSW index, embedding backfill (still deferred until model chosen) |
| Append-only LLM usage audit rows (no prompts/completions) | Live provider adapter, paid API calls |
| Atomic per-document token `reserve` / `reconcile` / `release` | Changing `FINLEDGER_PER_BILL_TOKEN_CAP` semantics (still env-driven, default `20000`) |
| SECURITY DEFINER functions callable from extract (and other AI) workers | Hosted apply, new Supabase roles, weakening RLS |

---

## 2. What already exists (do not recreate)

Migration `002_documents_queue.sql` already defines **`rag_chunks`** with RLS in `003_rls_functions.sql`:

| Column | Role today |
|---|---|
| `client_id` | Tenant key (RLS: `client_id = app_client_id()`) |
| `chunk_type` | Same enum as `packages/rag/chunk_types.py` |
| `ref` | Stable external reference string (default `''`) |
| `content` | Chunk text for lexical ranker |
| `metadata` | `jsonb` — ranker fields (GSTIN, HSN, amount, doc type, etc.) |
| `embedding` | `vector` or `real[]` when pgvector absent |

Person 1’s in-memory API uses **`chunk_id`**; the database column is **`ref`**. Adapters map `chunk_id` ↔ `ref` on read/write. Logical namespace `client_{client_id}` stays in Python (`build_client_namespace`); isolation is `client_id` + RLS, not a shared vector index.

**Gap vs current Python upsert:** no uniqueness on `(client_id, ref)`, so SQL `INSERT` cannot enforce upsert without a follow-up migration. Friend 2 should add:

```sql
-- Illustrative only — belongs in Friend 2's numbered migration, not applied here.
create unique index rag_chunks_client_ref on rag_chunks (client_id, ref);
alter table rag_chunks add column updated_at timestamptz not null default now();
-- Optional: trigger or application sets updated_at on upsert.
```

Empty `ref` is allowed today for tests; production upserts should always set `ref` to the Person 1 `chunk_id` (e.g. `correction:…`, `posted:{document_id}:{erp_id}`).

---

## 3. New tables (illustrative names)

Friend 2 may rename tables for consistency; behavior below is the contract.

### 3.1 `llm_usage_rows`

One row per completed model call after `reconcile` (matches `packages/usage/usage_writer.py` `UsageRow`). **No** prompt, completion, markdown, or invoice fields.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint` identity PK | |
| `client_id` | `uuid` not null → `clients` | RLS tenant |
| `document_id` | `uuid` not null | FK `(client_id, document_id)` → `documents` (composite, same pattern as `posting_attempts`) |
| `reservation_id` | `uuid` not null | Links to the hold that was reconciled |
| `purpose` | `text` not null | `extract`, `extract_vision`, `map`, `risk_llm`, `summary` |
| `model` | `text` not null | Operational label (mock-era `gemini-1.5-*` until ADR-001 activation) |
| `tokens_in` | `int` not null check (`tokens_in >= 0`) | |
| `tokens_out` | `int` not null check (`tokens_out >= 0`) | |
| `latency_ms` | `int` not null check (`latency_ms >= 0`) | |
| `cost_usd` | `numeric(12,6) not null default 0` | ADR-001 formula at write time |
| `created_at` | `timestamptz not null default now()` | Replaces Python `timestamp` ISO string |

Indexes (illustrative):

- `(client_id, document_id, created_at desc)` — per-bill audit and cap math
- `(client_id, created_at desc)` — firm-level reporting (future)

### 3.2 `llm_token_reservations`

Short-lived holds between `reserve` and `reconcile` or `release`.

| Column | Type | Notes |
|---|---|---|
| `reservation_id` | `uuid` primary key | Client-generated UUID (matches `KillSwitch.Reservation.reservation_id`) |
| `client_id` | `uuid` not null | |
| `document_id` | `uuid` not null | FK `(client_id, document_id)` → `documents` |
| `estimated_tokens` | `int` not null check (`estimated_tokens >= 0`) | Upper bound reserved before the call |
| `state` | `text` not null | `held` \| `reconciled` \| `released` |
| `created_at` | `timestamptz not null default now()` | |
| `updated_at` | `timestamptz not null default now()` | |

Partial unique index (optional hygiene): at most one `held` row per `reservation_id` (PK already enforces). For cap math, only `state = 'held'` rows count toward the bill budget.

---

## 4. Row Level Security and grants (match Person 2 style)

Follow `posting_attempts` / `003_rls_functions.sql` tenant loop pattern:

```sql
alter table llm_usage_rows enable row level security;
create policy tenant on llm_usage_rows
  using (client_id = app_client_id()) with check (client_id = app_client_id());

alter table llm_token_reservations enable row level security;
create policy tenant on llm_token_reservations
  using (client_id = app_client_id()) with check (client_id = app_client_id());
```

**`rag_chunks`:** RLS already enabled; no policy change required.

**Grants (illustrative):**

| Role | `rag_chunks` | `llm_usage_rows` | `llm_token_reservations` | Token functions (§5) |
|---|---|---|---|---|
| `finledger_app` | `select, insert, update, delete` (existing blanket grant may already cover; re-grant explicitly if Friend 2 narrows defaults) | `select` only (read audit under tenant) | none direct | `execute` on `release` only if ever needed from app — **prefer workers only** |
| `finledger_worker_extract` | `select, insert, update, delete` | `insert` | via functions only | `execute` on `reserve`, `reconcile`, `release` |
| `finledger_worker_score`, `finledger_worker_map` | same as extract if those workers call the model | `insert` | via functions | `execute` on token functions |

Map/score workers will need the same token and usage paths when they call `MockProvider` / future adapter outside extract-only jobs. Friend 2 can grant token functions to all `finledger_worker_*` roles that invoke LLM purposes, or to a dedicated `finledger_worker_ai` role — Friend 2’s call.

Do **not** grant token functions to `finledger_app` unless a future control UI needs read-only budget display (out of scope for this migration).

---

## 5. Atomic token cap functions (SECURITY DEFINER)

Cap source: application passes `p_cap` from `FINLEDGER_PER_BILL_TOKEN_CAP` (default `20000`). Kill switch off (`FINLEDGER_LLM_KILL_SWITCH` false): Python skips `reserve` entirely (unchanged behavior); functions are not called.

All functions:

- `set search_path = public, pg_temp`
- `security definer` (owner bypasses RLS; enforce `client_id` / `document_id` in function body)
- Verify `pg_has_role(session_user, 'finledger_worker_extract', 'MEMBER')` **or** the expanded worker set Friend 2 chooses
- Require `app.client_id` to match `p_client_id` when called inside `tenant()` (defense in depth)

### 5.1 `llm_reserve_tokens`

```text
llm_reserve_tokens(
  p_client_id uuid,
  p_document_id uuid,
  p_reservation_id uuid,
  p_estimated_tokens int,
  p_cap int
) returns void
```

**Algorithm (single transaction):**

1. Validate `p_estimated_tokens >= 0`, `p_cap > 0`.
2. Lock the document budget row:  
   `select 1 from documents where client_id = p_client_id and id = p_document_id for update`  
   (or dedicated summary row if Friend 2 prefers — document row lock is enough for `(client_id, document_id)` serialization).
3. `used :=` sum(`tokens_in + tokens_out`) from `llm_usage_rows` for that bill.
4. `held :=` sum(`estimated_tokens`) from `llm_token_reservations` where `state = 'held'`.
5. If `used + held + p_estimated_tokens > p_cap`, raise `TokenCapExceededException` equivalent (`errcode` / `message` without invoice text — mirror `packages/usage/kill_switch.py`).
6. `insert into llm_token_reservations (... state = 'held')`.

Two concurrent connections for the same bill: one blocks on step 2; the second fails step 5 if the first hold consumes the remainder.

### 5.2 `llm_reconcile_tokens`

```text
llm_reconcile_tokens(
  p_reservation_id uuid,
  p_actual_tokens int,
  p_tokens_in int,
  p_tokens_out int,
  p_latency_ms int,
  p_cost_usd numeric,
  p_purpose text,
  p_model text
) returns void
```

1. `select * from llm_token_reservations where reservation_id = p_reservation_id and state = 'held' for update`.
2. If not found → error (unknown or already settled).
3. Update reservation `state = 'reconciled'`.
4. `insert into llm_usage_rows` with actual token breakdown and metadata.
5. `p_actual_tokens` should equal `p_tokens_in + p_tokens_out` (enforce with check or raise).

Reconcile records **actual** tokens; the hold’s estimate is discarded from `held` totals.

### 5.3 `llm_release_tokens`

```text
llm_release_tokens(p_reservation_id uuid) returns void
```

1. Lock held row; if missing, no-op or error (match Python: unknown id raises in `reconcile`, `release` is tolerant in disabled switch — document choice: raise on unknown for symmetry with in-memory).
2. `state = 'released'`.

Failed `complete()` in `MockProvider` calls `release` in `except` (unchanged contract).

---

## 6. Python mapping (Friend 1, after migration lands)

| Component | Today | After migration |
|---|---|---|
| `Indexer.upsert` | In-memory dict | `INSERT ... ON CONFLICT (client_id, ref) DO UPDATE` on `rag_chunks`; set `metadata`, `content`, `chunk_type`, `updated_at` |
| `Retriever.retrieve` | Scan in-memory namespace | `select` tenant chunks; same lexical scoring in Python until embeddings exist |
| `UsageWriter.log_usage` | Append to list | Insert via `llm_reconcile_tokens` path (or direct insert only if reconcile always writes the row — prefer single write in reconcile) |
| `KillSwitch.reserve` | Thread lock | `llm_reserve_tokens` on DB conn inside `tenant()` |
| `KillSwitch.reconcile` / `release` | Thread lock | `llm_reconcile_tokens` / `llm_release_tokens` |

Keep in-memory implementations for unit tests that do not need Postgres. Restart and concurrency proofs use Person 2’s ephemeral Postgres harness as `finledger_app` (never `postgres` / service role for assertions).

**Connection usage:** extract consumer claims jobs outside tenant context, then sets `app.client_id` for reads/writes (per audit sketch in Friend 1 plan). Token functions still require matching `p_client_id` to `app.client_id`.

---

## 7. Metadata contract for `rag_chunks.metadata`

Align with `packages/rag/retriever.py` scoring (jsonb keys):

| Key | Used for |
|---|---|
| `gstin` | GSTIN match (+8) |
| `hsn` | HSN prefix match (+4) |
| `amount` | numeric band match (+3) |
| `doc_type` | document type match (+2) |
| `description` | lexical overlap |
| `document_id`, `erp_id`, `field` | traceability for memory/posted_bill chunks |

No invoice body beyond what is already in `content` for RAG (same as today). Friend 1 does not add ERP field names to extract/map contracts in this step.

---

## 8. Verification checklist (Friend 2 migration PR + Friend 1 follow-up)

Ephemeral Postgres, member of `finledger_app` / worker roles as appropriate:

1. **Restart:** upsert chunks + reconcile one usage row; new connection reads both under same `client_id`.
2. **Cross-tenant:** client A rows invisible with `app.client_id` = B; direct `select` as `finledger_app` returns zero foreign rows.
3. **Concurrent reservations:** two sessions race `llm_reserve_tokens` on the same bill near `p_cap`; at most one succeeds if `used + held + est` would exceed cap; loser gets cap error without invoice text.
4. **Release on failure:** held row returns to `released`; `held` sum drops; a later reserve can succeed.
5. **Kill switch off:** no DB reserve calls; behavior matches current tests.

---

## 9. Open questions for Friend 2

1. **Table names:** `llm_usage_rows` / `llm_token_reservations` vs `ai_*` prefix — pick one convention with existing `rag_chunks`.
2. **Worker grants:** grant token functions to all model-calling queues vs extract-only for v1.
3. **`rag_chunks.ref`:** backfill empty `ref` in dev DBs before unique index, or use `unique nulls not distinct` pattern if empty refs must remain in tests.
4. **Retention:** usage rows are operational metadata; align with owner retention decision (ADR-001) before staging customer data — no invoice payload either way.

---

## 10. References

- Friend 1 plan step 2: `docs/friend-1-plan.md` (project store) / team assignment `docs/assignments/01_FRIEND_AI.md`
- Existing DDL: `person2_platform/src/finledger_platform/migrations/002_documents_queue.sql` (`rag_chunks`), `005_security_workflow.sql` (composite FKs, worker roles)
- Python contracts: `packages/usage/usage_writer.py`, `packages/usage/kill_switch.py`, `packages/rag/indexer.py`, `packages/usage/provider.py`
- `HANDOVER.md` — migration `001`–`011` applied on staging; proposal only until Friend 2 merges SQL
