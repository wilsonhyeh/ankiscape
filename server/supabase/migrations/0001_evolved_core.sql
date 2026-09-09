-- 0001_evolved_core.sql - AnkiScape Evolved 3.0 core schema (contract F).
-- Private write access; clients go through RPCs only. Revoke table writes
-- from anon/authenticated; security-definer functions use fixed search_path.

create extension if not exists "pgcrypto";

-- Versioned rules snapshot (read-only content loaded from shared/rules-v1.json).
create table if not exists public.rules_versions (
  version int primary key,
  rules jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists public.players (
  user_id uuid primary key references auth.users(id) on delete cascade,
  username_norm text not null,
  username_display text not null,
  game_uuid uuid unique,
  status text not null default 'active' check (status in ('active','flagged','banned')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists players_username_norm_ux on public.players(username_norm);

create table if not exists public.game_operations (
  id bigint generated always as identity primary key,
  op_id uuid not null unique,
  game_uuid uuid not null,
  user_id uuid not null references public.players(user_id) on delete cascade,
  device_id text not null check (device_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  device_seq bigint not null check (device_seq > 0),
  lamport bigint not null check (lamport >= 0),
  kind text not null check (kind in ('review_award','review_retract','review_restore','catchup_preset')),
  payload jsonb not null,
  payload_hash text not null,
  created_at timestamptz not null default now(),
  unique (game_uuid, device_id, device_seq)
);
create index if not exists game_ops_canonical_ix
  on public.game_operations(game_uuid, lamport, device_id, device_seq, op_id);
create index if not exists game_ops_cursor_ix on public.game_operations(game_uuid, id);

create table if not exists public.game_state (
  game_uuid uuid primary key,
  user_id uuid not null references public.players(user_id) on delete cascade,
  revision bigint not null default 0,
  xp jsonb not null default '{}',
  inventory jsonb not null default '{}',
  achievements jsonb not null default '[]',
  counters jsonb not null default '{}',
  checkpoint jsonb not null default '{}',
  updated_at timestamptz not null default now()
);

create table if not exists public.game_checkpoints (
  game_uuid uuid not null,
  revision bigint not null,
  state jsonb not null,
  created_at timestamptz not null default now(),
  primary key (game_uuid, revision)
);

create table if not exists public.review_claims (
  game_uuid uuid not null,
  review_key text not null,
  provenance text not null check (provenance in ('direct','catchup')),
  retracted boolean not null default false,
  award_op_id uuid not null references public.game_operations(op_id) on delete cascade,
  updated_at timestamptz not null default now(),
  primary key (game_uuid, review_key)
);
create index if not exists review_claims_game_ix on public.review_claims(game_uuid);

create table if not exists public.rate_limits (
  bucket text not null,
  window_start timestamptz not null,
  count int not null default 0,
  primary key (bucket, window_start)
);

create table if not exists public.moderation_audit (
  id bigint generated always as identity primary key,
  target_user_id uuid,
  action text not null,
  reason text not null default '',
  actor text not null default 'maintainer-cli',
  created_at timestamptz not null default now()
);

alter table public.rules_versions enable row level security;
alter table public.players enable row level security;
alter table public.game_operations enable row level security;
alter table public.game_state enable row level security;
alter table public.game_checkpoints enable row level security;
alter table public.review_claims enable row level security;
alter table public.rate_limits enable row level security;
alter table public.moderation_audit enable row level security;

-- No direct table access for anon/authenticated; RPCs only.
revoke all on public.rules_versions from anon, authenticated;
revoke all on public.players from anon, authenticated;
revoke all on public.game_operations from anon, authenticated;
revoke all on public.game_state from anon, authenticated;
revoke all on public.game_checkpoints from anon, authenticated;
revoke all on public.review_claims from anon, authenticated;
revoke all on public.rate_limits from anon, authenticated;
revoke all on public.moderation_audit from anon, authenticated;

-- Read-only rules for authenticated clients (current version row).
create policy rules_read_authenticated on public.rules_versions
  for select to authenticated using (true);

-- Thresholds for level derivation live in rules_versions content; this helper
-- mirrors the golden vectors for cross-language tests (XP in micro units).
create or replace function public.evolved_level(xp_micro bigint, thresholds bigint[])
returns int
language sql immutable
set search_path = public
as $$
  select coalesce(max(idx), 1) from (
    select row_number() over () as idx, t from unnest(thresholds) with ordinality as u(t, o)
  ) s(idx, t) where xp_micro >= t * 1000000
$$;

revoke all on function public.evolved_level(bigint, bigint[]) from anon, authenticated;
grant execute on function public.evolved_level(bigint, bigint[]) to authenticated;
