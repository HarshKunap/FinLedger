-- Ephemeral test overlay only (not a numbered platform migration).

create unique index if not exists rag_chunks_client_ref on rag_chunks (client_id, ref);

create table if not exists llm_usage_rows (
  id bigint generated always as identity primary key,
  client_id uuid not null references clients(id),
  document_id uuid not null,
  reservation_id uuid not null,
  purpose text not null,
  model text not null,
  tokens_in int not null check (tokens_in >= 0),
  tokens_out int not null check (tokens_out >= 0),
  latency_ms int not null check (latency_ms >= 0),
  cost_usd numeric(12,6) not null default 0,
  created_at timestamptz not null default now(),
  foreign key (client_id, document_id) references documents(client_id, id) on delete cascade
);

create table if not exists llm_token_reservations (
  reservation_id uuid primary key,
  client_id uuid not null,
  document_id uuid not null,
  estimated_tokens int not null check (estimated_tokens >= 0),
  state text not null check (state in ('held', 'reconciled', 'released')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (client_id, document_id) references documents(client_id, id) on delete cascade
);

alter table llm_usage_rows enable row level security;
drop policy if exists tenant on llm_usage_rows;
create policy tenant on llm_usage_rows
  using (client_id = app_client_id()) with check (client_id = app_client_id());

alter table llm_token_reservations enable row level security;
drop policy if exists tenant on llm_token_reservations;
create policy tenant on llm_token_reservations
  using (client_id = app_client_id()) with check (client_id = app_client_id());

grant select on llm_usage_rows to finledger_app;
grant select, insert on llm_usage_rows to finledger_worker_extract;
grant select, insert, update on llm_token_reservations to finledger_worker_extract;

create or replace function llm_reserve_tokens(
  p_client_id uuid,
  p_document_id uuid,
  p_reservation_id uuid,
  p_estimated_tokens int,
  p_cap int
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  used int;
  held int;
begin
  if p_estimated_tokens < 0 or p_cap <= 0 then
    raise exception 'invalid_cap_arguments';
  end if;
  if app_client_id() is distinct from p_client_id then
    raise exception 'tenant_mismatch';
  end if;
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
end;
$$;

create or replace function llm_reconcile_tokens(p_reservation_id uuid) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  update llm_token_reservations
    set state = 'reconciled', updated_at = now()
    where reservation_id = p_reservation_id and state = 'held';
  if not found then
    raise exception 'unknown_reservation';
  end if;
end;
$$;

create or replace function llm_release_tokens(p_reservation_id uuid) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  update llm_token_reservations
    set state = 'released', updated_at = now()
    where reservation_id = p_reservation_id and state = 'held';
end;
$$;

grant execute on function llm_reserve_tokens(uuid, uuid, uuid, int, int) to finledger_worker_extract;
grant execute on function llm_reconcile_tokens(uuid) to finledger_worker_extract;
grant execute on function llm_release_tokens(uuid) to finledger_worker_extract;
