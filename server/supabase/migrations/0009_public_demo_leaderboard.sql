-- 0009_public_demo_leaderboard.sql - Public rankings with labeled demos.
-- Append-only migration: never edit 0001-0008 as a rollout strategy.
--
-- Adds:
--   * players.is_demo - privileged-only permanent demo flag. Never settable
--     from client metadata or any authenticated RPC (same guard pattern as
--     is_test).
--   * Public ranking/profile allowlists: rows are exactly
--     {rank, username, xp, is_demo} and profiles exactly
--     {username, is_demo, state:{xp}} - no auth UUID, game UUID, inventory,
--     checkpoint, counters, email, registry fields or timestamps.
--
-- Public visible membership = active real players (is_test = false) plus the
-- exact published demo identities (is_demo = true). Temporary isolated test
-- accounts stay excluded. The filter runs before ranking, sorting and
-- limiting; demos rank under the same scoring/ranking/tie rules as everyone
-- else (never pinned above better-ranked players).

-- ---------------------------------------------------------------------------
-- 1. Demo flag (privileged only; invisible to ordinary RPCs).
-- ---------------------------------------------------------------------------
alter table public.players
  add column if not exists is_demo boolean not null default false;

create or replace function public._guard_demo_flag()
returns trigger
language plpgsql security definer
set search_path = public
as $$
begin
  if NEW.is_demo is distinct from OLD.is_demo and auth.uid() is not null then
    raise exception 'demo_flag_privileged' using errcode = '42501';
  end if;
  return NEW;
end;
$$;
revoke all on function public._guard_demo_flag() from public, anon, authenticated;

drop trigger if exists ankiscape_guard_demo_flag on public.players;
create trigger ankiscape_guard_demo_flag
  before update on public.players
  for each row execute function public._guard_demo_flag();

-- ---------------------------------------------------------------------------
-- 2. Public ranking helper (allowlisted fields only).
-- ---------------------------------------------------------------------------
create or replace function public._hiscores_public(p_skill text, p_limit int)
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
  select coalesce(jsonb_agg(jsonb_build_object(
           'rank', t.rank,
           'username', t.username,
           'xp', t.xp,
           'is_demo', t.is_demo) order by t.xp desc, t.username_norm, t.user_id),
         '[]'::jsonb)
    into v_rows from (
      select p.username_display as username, p.username_norm, p.user_id,
             p.is_demo,
             coalesce(((s.xp ->> p_skill)::bigint), 0) as xp,
             rank() over (order by coalesce(((s.xp ->> p_skill)::bigint), 0) desc) as rank
      from public.players p join public.game_state s on s.game_uuid = p.game_uuid
      where p.status = 'active' and p.game_uuid is not null
        and (p.is_test = false or p.is_demo = true)
      order by xp desc, p.username_norm, p.user_id
      limit v_lim) t;
  return v_rows;
end;
$$;
revoke all on function public._hiscores_public(text, int)
  from public, anon, authenticated;

create or replace function public._profile_public(p_username_norm text)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_name text;
  v_demo boolean;
  v_xp jsonb;
begin
  select p.username_display, p.is_demo, coalesce(s.xp, '{}'::jsonb)
    into v_name, v_demo, v_xp
  from public.players p
  join public.game_state s on s.game_uuid = p.game_uuid
  where p.username_norm = lower(trim(p_username_norm)) and p.status = 'active'
    and (p.is_test = false or p.is_demo = true);
  if v_name is null then
    return null;
  end if;
  -- Minimal wrapper retained for old clients; only the per-skill xp table.
  return jsonb_build_object(
    'username', v_name,
    'is_demo', coalesce(v_demo, false),
    'state', jsonb_build_object('xp', v_xp));
end;
$$;
revoke all on function public._profile_public(text)
  from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. Public wrappers (anon + authenticated), same signatures as before.
-- ---------------------------------------------------------------------------
create or replace function public.hiscores(p_skill text, p_limit int)
returns jsonb
language sql security definer
set search_path = public
as $$
  select public._hiscores_public(p_skill, p_limit);
$$;
revoke all on function public.hiscores(text, int) from public, anon, authenticated;
grant execute on function public.hiscores(text, int) to anon, authenticated;

create or replace function public.public_profile(p_username_norm text)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_result jsonb;
begin
  v_result := public._profile_public(p_username_norm);
  if v_result is null then
    -- Test-only and unknown names are indistinguishable here.
    raise exception 'no_profile' using errcode = '02000';
  end if;
  return v_result;
end;
$$;
revoke all on function public.public_profile(text) from public, anon, authenticated;
grant execute on function public.public_profile(text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 4. Capability marker.
-- ---------------------------------------------------------------------------
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
    'public_demos', true,
    'public_board', 'v2',
    'test_rpcs', jsonb_build_array('self_context','test_hiscores',
                                   'test_public_profile'),
    'operation_kinds', jsonb_build_array(
      'review_award','review_retract','review_restore','catchup_preset','review_skip'))
$$;
revoke all on function public.evolved_capabilities() from public, anon, authenticated;
grant execute on function public.evolved_capabilities() to anon, authenticated;

-- Re-assert grants the wrapper replacements may have dropped.
revoke execute on function public.hiscores(text, int) from public;
grant execute on function public.hiscores(text, int) to anon, authenticated;
revoke execute on function public.public_profile(text) from public;
grant execute on function public.public_profile(text) to anon, authenticated;
