-- Shotgun Analytics — Supabase schema
-- Apply once via Supabase SQL editor (Database → SQL Editor → Run)
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Ingressos Shotgun (cache por usuário, um row por ingresso)
create table if not exists shotgun_tickets (
  id          bigserial primary key,
  user_id     uuid        not null references auth.users(id) on delete cascade,
  ticket_id   text        not null,           -- ID do ingresso na API Shotgun
  raw         jsonb       not null,           -- registro completo (flexível)
  fetched_at  timestamptz not null default now(),
  unique (user_id, ticket_id)
);
create index if not exists shotgun_tickets_user_idx on shotgun_tickets (user_id);

-- RLS: cada usuário vê/edita apenas seus próprios registros
alter table shotgun_tickets enable row level security;
drop policy if exists "own rows" on shotgun_tickets;
create policy "own rows" on shotgun_tickets
  for all
  using  (user_id = auth.uid())
  with check (user_id = auth.uid());


-- 2. Entradas Porta (substitui porta_entries.json, escopo por usuário)
create table if not exists porta_entries (
  id           bigserial primary key,
  user_id      uuid           not null references auth.users(id) on delete cascade,
  event_name   text           not null,
  tickets      int            not null,
  revenue_brl  numeric(12,2)  not null,
  prices       jsonb,                         -- list[float] com preços individuais
  entry_date   date,                          -- quando usado modo por data (PagBank)
  source       text           not null check (source in ('manual','pagbank_csv','consolidated_upload')),
  added_at     timestamptz    not null default now()
);
create index if not exists porta_entries_user_idx on porta_entries (user_id);

-- RLS
alter table porta_entries enable row level security;
drop policy if exists "own rows" on porta_entries;
create policy "own rows" on porta_entries
  for all
  using  (user_id = auth.uid())
  with check (user_id = auth.uid());
