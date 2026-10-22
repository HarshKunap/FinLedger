-- Durable AI state (docs/proposals/AI_DURABLE_RETRIEVAL_USAGE_SCHEMA.md) and the per-firm automation maker.
--
--   rag_chunks             unique (client_id, ref) so PostgresIndexer can upsert; empty refs get a generated one
--   llm_usage_rows         append-only model-call metadata (no prompts, completions or invoice fields)
--   llm_token_reservations per-bill holds; only 'held' rows count toward the cap
--   llm_*_tokens()         atomic reserve / reconcile / release for model-calling workers
--   users.automation       one non-login "FinLedger automation" maker per firm (owner decision 2026-10-10):
--                          no API token, no Auth subject, no firm-admin flag, no client roles
--
-- Function signatures match packages/usage/postgres_kill_switch.py. Nothing here grants posting or payment.

-- ---------- rag_chunks: (client_id, ref) uniqueness ----------

update rag_chunks set ref = 'legacy:' || id where ref = '';

do $$
declare dupes int;
begin
  select count(*) into dupes from (
    select 1 from rag_chunks group by client_id, ref having count(*) > 1) d;
  if dupes > 0 then
    raise exception 'rag_chunks has % duplicate (client_id, ref) groups; resolve them before applying 012', dupes;
  end if;
end $$;

create unique index rag_chunks_client_ref on rag_chunks (client_id, ref);
alter table rag_chunks add column updated_at timestamptz not null default now();

-- Callers that omit ref still insert; ON CONFLICT (client_id, ref) needs a full (non-partial) unique index.
create function rag_chunks_fill_ref() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if new.ref = '' then
    new.ref := 'auto:' || gen_random_uuid();
  end if;
  if tg_op = 'UPDATE' then
    new.updated_at := now();
  end if;
  return new;
end $$;
create trigger rag_chunks_fill_ref before insert or update on rag_chunks
  for each row execute function rag_chunks_fill_ref();

-- ---------- usage and reservations ----------

create table llm_usage_rows (
  id              bigint generated always as identity primary key,
  client_id       uuid not null references clients(id) on delete cascade,
  document_id     uuid not null,
  reservation_id  uuid not null,
  purpose         text not null check (purpose in ('extract', 'extract_vision', 'map', 'risk_llm', 'summary')),
  model           text not null check (length(model) between 1 and 200),
  tokens_in       int not null check (tokens_in >= 0),
  tokens_out      int not null check (tokens_out >= 0),
  latency_ms      int not null check (latency_ms >= 0),
  cost_usd        numeric(12,6) not null default 0 check (cost_usd >= 0),
  created_at      timestamptz not null default now(),
  foreign key (client_id, document_id) references documents(client_id, id) on delete cascade
);
create index llm_usage_bill on llm_usage_rows (client_id, document_id, created_at desc);
create index llm_usage_client on llm_usage_rows (client_id, created_at desc);

create table llm_token_reservations (
  reservation_id    uuid primary key,
  client_id         uuid not null references clients(id) on delete cascade,
  document_id       uuid not null,
  estimated_tokens  int not null check (estimated_tokens >= 0),
  state             text not null default 'held' check (state in ('held', 'reconciled', 'released')),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  foreign key (client_id, document_id) references documents(client_id, id) on delete cascade
);
create index llm_reservations_held on llm_token_reservations (client_id, document_id) where state = 'held';

alter table llm_usage_rows enable row level security;
create policy tenant on llm_usage_rows
  using (client_id = app_client_id()) with check (client_id = app_client_id());
alter table llm_token_reservations enable row level security;
create policy tenant on llm_token_reservations
  using (client_id = app_client_id()) with check (client_id = app_client_id());

-- Model-calling workers: the extract, score and map queues.
create function llm_worker_required() returns void
language plpgsql stable set search_path = public, pg_temp as $$
begin
  if not (pg_has_role(session_user, 'finledger_worker_extract', 'MEMBER')
          or pg_has_role(session_user, 'finledger_worker_score', 'MEMBER')
          or pg_has_role(session_user, 'finledger_worker_map', 'MEMBER')) then
    raise exception 'model worker role required' using errcode = '42501';
  end if;
end $$;

create function llm_reserve_tokens(p_client_id uuid, p_document_id uuid, p_reservation_id uuid,
                                   p_estimated_tokens int, p_cap int) returns void
language plpgsql volatile security definer set search_path = public, pg_temp as $$
declare used bigint; held bigint;
begin
  perform llm_worker_required();
  if p_estimated_tokens is null or p_cap is null or p_estimated_tokens < 0 or p_cap <= 0 then
    raise exception 'invalid_cap_arguments';
  end if;
  if app_client_id() is distinct from p_client_id then
    raise exception 'tenant_mismatch';
  end if;
  -- The document row lock serializes concurrent reservations for one bill.
  perform 1 from documents where client_id = p_client_id and id = p_document_id for update;
  if not found then
    raise exception 'document_not_found';
  end if;
  select coalesce(sum(tokens_in + tokens_out), 0) into used
    from llm_usage_rows where client_id = p_client_id and document_id = p_document_id;
  select coalesce(sum(estimated_tokens), 0) into held
    from llm_token_reservations
   where client_id = p_client_id and document_id = p_document_id and state = 'held';
  if used + held + p_estimated_tokens > p_cap then
    raise exception 'ai_budget_exceeded';
  end if;
  insert into llm_token_reservations (reservation_id, client_id, document_id, estimated_tokens, state)
    values (p_reservation_id, p_client_id, p_document_id, p_estimated_tokens, 'held');
end $$;

create function llm_reconcile_tokens(p_reservation_id uuid) returns void
language plpgsql volatile security definer set search_path = public, pg_temp as $$
begin
  perform llm_worker_required();
  update llm_token_reservations set state = 'reconciled', updated_at = now()
   where reservation_id = p_reservation_id and state = 'held' and client_id = app_client_id();
  if not found then
    raise exception 'unknown_reservation';
  end if;
end $$;

-- Tolerant of unknown or already-settled ids: release runs on failure paths.
create function llm_release_tokens(p_reservation_id uuid) returns void
language plpgsql volatile security definer set search_path = public, pg_temp as $$
begin
  perform llm_worker_required();
  update llm_token_reservations set state = 'released', updated_at = now()
   where reservation_id = p_reservation_id and state = 'held' and client_id = app_client_id();
end $$;

-- ---------- automation maker ----------

alter table users add column automation boolean not null default false;
alter table users add constraint users_automation_no_login
  check (not automation or (api_token_hash is null and auth_subject is null and not firm_admin));
create unique index users_one_automation_per_firm on users (firm_id) where automation;

create function automation_maker_email(p_firm_id uuid) returns text
language sql immutable set search_path = public, pg_temp as $$
  select 'automation+' || p_firm_id::text || '@automation.finledger.invalid'
$$;

create function ensure_firm_automation_maker() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  insert into users (firm_id, email, name, automation)
    values (new.id, automation_maker_email(new.id), 'FinLedger automation', true)
    on conflict do nothing;
  return new;
end $$;
create trigger firms_automation_maker after insert on firms
  for each row execute function ensure_firm_automation_maker();

insert into users (firm_id, email, name, automation)
  select f.id, automation_maker_email(f.id), 'FinLedger automation', true from firms f
  on conflict do nothing;

-- The maker never holds clerk, approver or payer roles.
create function client_members_reject_automation() returns trigger
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if exists (select 1 from users where id = new.user_id and automation) then
    raise exception 'automation maker cannot hold client roles' using errcode = '42501';
  end if;
  return new;
end $$;
create trigger client_members_reject_automation before insert or update on client_members
  for each row execute function client_members_reject_automation();

-- Users are firm-scoped under RLS; workers run with only app.client_id, so they resolve the maker here.
create function automation_maker_id(p_client_id uuid) returns uuid
language plpgsql stable security definer set search_path = public, pg_temp as $$
declare maker uuid;
begin
  perform llm_worker_required();
  if app_client_id() is distinct from p_client_id then
    raise exception 'tenant_mismatch';
  end if;
  select u.id into maker from users u join clients c on c.firm_id = u.firm_id
   where c.id = p_client_id and u.automation;
  if maker is null then
    raise exception 'automation_maker_missing';
  end if;
  return maker;
end $$;

-- ---------- grants (least privilege; table writes for reservations go through the functions) ----------

grant select on llm_usage_rows to finledger_app;
grant insert on llm_usage_rows to finledger_worker_extract, finledger_worker_score, finledger_worker_map;
grant select on llm_token_reservations to finledger_worker_extract, finledger_worker_score, finledger_worker_map;

revoke execute on function llm_worker_required(), llm_reserve_tokens(uuid,uuid,uuid,int,int),
  llm_reconcile_tokens(uuid), llm_release_tokens(uuid), automation_maker_id(uuid),
  automation_maker_email(uuid), rag_chunks_fill_ref(), ensure_firm_automation_maker(),
  client_members_reject_automation() from public, finledger_app;
grant execute on function llm_reserve_tokens(uuid,uuid,uuid,int,int),
  llm_reconcile_tokens(uuid), llm_release_tokens(uuid), automation_maker_id(uuid)
  to finledger_worker_extract, finledger_worker_score, finledger_worker_map;
