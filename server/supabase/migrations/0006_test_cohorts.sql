-- 0006_test_cohorts.sql - Permanent hosted test players, isolated by cohort.
-- Append-only migration: never edit 0001-0005 as a rollout strategy.
--
-- Adds:
--   * players.is_test                - privileged-only cohort flag.
--   * public.fixture_registry        - private fixture identity registry.
--   * cohort-guarded ranking helpers - hiscores/public_profile filter BEFORE
--                                      rank/sort/limit; test rows are never
--                                      visible on public paths.
--   * test_hiscores / test_public_profile / self_context - authenticated test
--                                      cohort RPCs (caller must BE a test row;
--                                      a client flag never authorizes).
--
-- Authority rules are unchanged: both cohorts share the same scoring/upload
-- logic; this is not a second scoring backend.

-- ---------------------------------------------------------------------------
-- 1. Cohort flag + fixture registry (private; service-role tooling only).
-- ---------------------------------------------------------------------------
alter table public.players
  add column if not exists is_test boolean not null default false;

create table if not exists public.fixture_registry (
  suite_id text not null,
  suite_version int not null,
  username_norm text not null,
  user_id uuid unique references auth.users(id) on delete restrict,
  game_uuid uuid,
  expected_trace_hash text not null default '',
  seed_state text not null default 'reserved'
    check (seed_state in ('reserved','provisioned','seeded','verified')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (suite_id, suite_version, username_norm)
);
create unique index if not exists fixture_registry_trace_ux
  on public.fixture_registry(suite_id, suite_version, expected_trace_hash)
  where expected_trace_hash <> '';

alter table public.fixture_registry enable row level security;
revoke all on public.fixture_registry from anon, authenticated;

-- Reserved fixture names can never be claimed by an ordinary signup: the
-- name is held for the suite before its account exists.
create or replace function public._claim_username_before()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
  v_norm text;
begin
  v_norm := nullif(trim(both from lower(NEW.raw_user_meta_data ->> 'username_norm')), '');
  if v_norm is null then
    return NEW;  -- non-AnkiScape signup; untouched
  end if;
  if v_norm !~ '^[a-z0-9_]{3,20}$' then
    raise exception 'invalid_username' using errcode = '22000';
  end if;
  if exists (select 1 from public.players p where p.username_norm = v_norm) then
    raise exception 'username_taken' using errcode = '23505';
  end if;
  if exists (select 1 from public.fixture_registry r
             where r.username_norm = v_norm and r.user_id is null) then
    raise exception 'username_taken' using errcode = '23505';
  end if;
  return NEW;
end;
$$;
revoke all on function public._claim_username_before() from public, anon, authenticated;

-- Classification cannot change through an authenticated request: only
-- privileged tooling (no auth.uid(), e.g. service role / SQL editor) may
-- set or clear is_test. Client metadata is ignored entirely (see the
-- username-claim trigger below, which only consults the registry).
create or replace function public._guard_test_flag()
returns trigger
language plpgsql security definer
set search_path = public
as $$
begin
  if NEW.is_test is distinct from OLD.is_test and auth.uid() is not null then
    raise exception 'test_flag_privileged' using errcode = '42501';
  end if;
  return NEW;
end;
$$;
revoke all on function public._guard_test_flag() from public, anon, authenticated;

drop trigger if exists ankiscape_guard_test_flag on public.players;
create trigger ankiscape_guard_test_flag
  before update on public.players
  for each row execute function public._guard_test_flag();

-- A reserved fixture username is born classified: the registration trigger
-- consults the registry (never client metadata) at player-row creation time,
-- so no public player state can exist before classification.
create or replace function public._claim_username_after()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
  v_norm text;
  v_display text;
  v_is_test boolean := false;
begin
  v_norm := nullif(trim(both from lower(NEW.raw_user_meta_data ->> 'username_norm')), '');
  if v_norm is null then
    return NEW;
  end if;
  v_display := coalesce(nullif(NEW.raw_user_meta_data ->> 'username_display', ''), v_norm);
  v_is_test := exists (
    select 1 from public.fixture_registry r
    where r.username_norm = v_norm);
  insert into public.players(user_id, username_norm, username_display, is_test)
  values (NEW.id, v_norm, left(v_display, 64), v_is_test);
  update public.fixture_registry
     set user_id = NEW.id, updated_at = now()
   where username_norm = v_norm and user_id is null;
  return NEW;
end;
$$;
revoke all on function public._claim_username_after() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. Cohort-parameterized ranking helpers (shared by both cohorts).
-- ---------------------------------------------------------------------------
create or replace function public._hiscores_cohort(p_skill text, p_limit int,
                                                   p_is_test boolean)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_lim int := least(greatest(coalesce(p_limit, 50), 1), 100);
  v_rows jsonb;
begin
  if p_skill not in ('mining','woodcutting','smithing','crafting','fishing','cooking') then
    raise exception 'bad_skill' using errcode = '22000';
  end if;
  select coalesce(jsonb_agg(t order by t.xp desc, t.username_norm, t.user_id), '[]'::jsonb)
    into v_rows from (
      select p.username_display as username, p.username_norm, p.user_id,
             coalesce(((s.xp ->> p_skill)::bigint), 0) as xp,
             rank() over (order by coalesce(((s.xp ->> p_skill)::bigint), 0) desc) as rank
      from public.players p join public.game_state s on s.game_uuid = p.game_uuid
      where p.status = 'active' and p.game_uuid is not null
        and p.is_test = p_is_test
      order by xp desc, p.username_norm, p.user_id
      limit v_lim) t;
  return v_rows;
end;
$$;
revoke all on function public._hiscores_cohort(text, int, boolean) from public, anon, authenticated;

create or replace function public._profile_cohort(p_username_norm text,
                                                  p_is_test boolean)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_state jsonb;
  v_name text;
begin
  select p.username_display, to_jsonb(s) into v_name, v_state
  from public.players p join public.game_state s on s.game_uuid = p.game_uuid
  where p.username_norm = lower(trim(p_username_norm)) and p.status = 'active'
    and p.is_test = p_is_test;
  if v_state is null then
    return null;
  end if;
  return jsonb_build_object('username', v_name, 'state', v_state);
end;
$$;
revoke all on function public._profile_cohort(text, boolean) from public, anon, authenticated;

-- Public paths: is_test = false before rank calculation, sort and limit.
create or replace function public.hiscores(p_skill text, p_limit int)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
begin
  return public._hiscores_cohort(p_skill, p_limit, false);
end;
$$;
revoke all on function public.hiscores(text, int) from anon, authenticated;
grant execute on function public.hiscores(text, int) to anon, authenticated;

create or replace function public.public_profile(p_username_norm text)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_result jsonb;
begin
  v_result := public._profile_cohort(p_username_norm, false);
  if v_result is null then
    -- Test profiles and unknown names are indistinguishable here.
    raise exception 'no_profile' using errcode = '02000';
  end if;
  return v_result;
end;
$$;
revoke all on function public.public_profile(text) from anon, authenticated;
grant execute on function public.public_profile(text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. Test-cohort RPCs (authenticated AND the caller's own row is_test).
-- ---------------------------------------------------------------------------
create or replace function public.self_context()
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_context jsonb;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select jsonb_build_object('username', p.username_display, 'is_test', p.is_test)
    into v_context
    from public.players p where p.user_id = v_user;
  if v_context is null then
    raise exception 'no_profile' using errcode = '02000';
  end if;
  return v_context;
end;
$$;
revoke all on function public.self_context() from public, anon, authenticated;
grant execute on function public.self_context() to authenticated;

create or replace function public.test_hiscores(p_skill text, p_limit int)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_is_test boolean;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select p.is_test into v_is_test from public.players p where p.user_id = v_user;
  if v_is_test is not true then
    -- Same response as an unknown target: never leak which cohort exists.
    raise exception 'no_profile' using errcode = '02000';
  end if;
  return public._hiscores_cohort(p_skill, p_limit, true);
end;
$$;
revoke all on function public.test_hiscores(text, int) from public, anon, authenticated;
grant execute on function public.test_hiscores(text, int) to authenticated;

-- Safe allowlist only: username plus per-skill xp/level from rules. Never the
-- full private game state, checkpoints, counters or operation ids.
create or replace function public.test_public_profile(p_username_norm text)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_is_test boolean;
  v_target public.players%rowtype;
  v_state jsonb;
  v_thresholds bigint[];
  v_skills jsonb;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select p.is_test into v_is_test from public.players p where p.user_id = v_user;
  if v_is_test is not true then
    raise exception 'no_profile' using errcode = '02000';
  end if;
  select * into v_target from public.players p
   where p.username_norm = lower(trim(p_username_norm))
     and p.status = 'active' and p.is_test = true;
  if not found then
    raise exception 'no_profile' using errcode = '02000';
  end if;
  select s.xp into v_state from public.game_state s
   where s.game_uuid = v_target.game_uuid;
  v_thresholds := (
    select array_agg(x::bigint)
    from jsonb_array_elements_text(public.evolved_rules() -> 'thresholds') x);
  select jsonb_object_agg(skill, jsonb_build_object(
           'xp', coalesce((v_state ->> skill)::bigint, 0),
           'level', public.evolved_level(
             coalesce((v_state ->> skill)::bigint, 0), v_thresholds)))
    into v_skills
    from unnest(array['mining','woodcutting','smithing','crafting',
                      'fishing','cooking']) as skills(skill);
  return jsonb_build_object('username', v_target.username_display,
                            'skills', v_skills,
                            'is_test', true);
end;
$$;
revoke all on function public.test_public_profile(text) from public, anon, authenticated;
grant execute on function public.test_public_profile(text) to authenticated;

-- Capability marker: clients detect cohort support before offering the
-- Test leaderboard; absence means the old server (public paths only).
create or replace function public.evolved_capabilities()
returns jsonb
language sql
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'protocol_version', 2,
    'reward_policy_max', 2,
    'authoritative_scoring', true,
    'test_cohorts', true,
    'test_rpcs', jsonb_build_array('self_context','test_hiscores',
                                   'test_public_profile'),
    'operation_kinds', jsonb_build_array(
      'review_award','review_retract','review_restore','catchup_preset','review_skip'))
$$;
revoke execute on function public.evolved_capabilities() from public;
grant execute on function public.evolved_capabilities() to anon, authenticated;

revoke execute on function public.hiscores(text, int) from public;
grant execute on function public.hiscores(text, int) to anon, authenticated;
revoke execute on function public.public_profile(text) from public;
grant execute on function public.public_profile(text) to anon, authenticated;
