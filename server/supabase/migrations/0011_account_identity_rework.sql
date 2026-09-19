-- 0011_account_identity_rework.sql - 3.0 account identity rework (D5/D7).
-- Append-only migration: never edit 0001-0010 as a rollout strategy.
--
-- Re-issues (all SECURITY DEFINER with a fixed search_path):
--   * public.players.visible_on_board - additive column, not null default true.
--   * public.link_game - returns the ACCOUNT's game, creating one when the
--     account has none. The server owns the game uuid; p_game_uuid is retained
--     for wire compatibility and ignored for identity (D5). Never raises
--     game_mismatch or game_claimed. Reply: {game_uuid, created, resumed} with
--     resumed == not created (first link {true,false}; every later link
--     {false,true}).
--   * public.set_board_visibility - writes the caller's own visibility flag.
--   * public.self_context - gains visible_on_board (re-issued from 0006).
--   * public._hiscores_public / public._profile_public - board visibility
--     filters the PUBLIC predicates only (0009:72 and 0009:96). The 0006
--     test-cohort helpers and RPCs (test_hiscores, test_public_profile) are
--     deliberately untouched.
--   * public.evolved_capabilities - advertises board_visibility.
--
-- The ownership guards in public.submit_operations (0004:894-897),
-- public.fetch_operations (0002:126-129) and public.get_game_state
-- (0002:156-159) are deliberately NOT re-issued: they are retained unchanged
-- as a security net. The normal path cannot reach them, because every caller
-- first calls link_game, which returns the account's own game uuid, and every
-- later submit/fetch/state call passes that returned uuid (the client-offered
-- uuid is dead after the first link). A guard hit therefore means a caller
-- deliberately presented a game uuid it does not own - the residual risk that
-- the S9 negative pgTAP case pins
-- (server/supabase/tests/0007_account_identity.test.sql). Offline review ops
-- are client-asserted facts by design; the guard is the containment, not a
-- verification mechanism.

-- ---------------------------------------------------------------------------
-- 1. Board visibility (D7): server-side flag; public predicates only.
-- ---------------------------------------------------------------------------
alter table public.players
  add column if not exists visible_on_board boolean not null default true;

-- ---------------------------------------------------------------------------
-- 2. link_game: the server owns the account game (D5). Re-issues the live
--    0003 body (0003_registration_gate.sql:81-118); 0002's body is superseded.
-- ---------------------------------------------------------------------------
create or replace function public.link_game(p_game_uuid uuid)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_row public.players%rowtype;
  v_game uuid;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  perform public._assert_verified(v_user);
  -- p_game_uuid is retained for wire compatibility and ignored for identity:
  -- 3.0.0 is unpublished, so the server mints and owns the game uuid. No
  -- invalid_game / game_claimed / game_mismatch can be raised here.
  select * into v_row from public.players where user_id = v_user for update;
  if not found then
    raise exception 'no_profile' using errcode = '28000';
  end if;
  if v_row.game_uuid is not null then
    return jsonb_build_object('game_uuid', v_row.game_uuid,
                              'created', false, 'resumed', true);
  end if;
  v_game := gen_random_uuid();
  update public.players set game_uuid = v_game, updated_at = now()
    where user_id = v_user;
  insert into public.game_state(game_uuid, user_id) values (v_game, v_user)
    on conflict (game_uuid) do nothing;
  return jsonb_build_object('game_uuid', v_game,
                            'created', true, 'resumed', false);
end;
$$;
revoke all on function public.link_game(uuid) from anon, authenticated;
grant execute on function public.link_game(uuid) to authenticated;

-- ---------------------------------------------------------------------------
-- 3. Board visibility toggle (D7). security definer is required: public.players
--    is fully revoked from authenticated (0001_evolved_core.sql:99-107). No
--    _assert_verified (a preference, not a game mutation - unverified users
--    cannot be on the board) and no privileged guard trigger.
-- ---------------------------------------------------------------------------
create or replace function public.set_board_visibility(p_visible boolean)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  if p_visible is null then
    raise exception 'invalid_visibility' using errcode = '22023';
  end if;
  update public.players
     set visible_on_board = p_visible, updated_at = now()
   where user_id = v_user;
  if not found then
    raise exception 'no_profile' using errcode = '28000';
  end if;
  return jsonb_build_object('visible_on_board', p_visible);
end;
$$;
revoke all on function public.set_board_visibility(boolean)
  from public, anon, authenticated;
grant execute on function public.set_board_visibility(boolean)
  to authenticated;

-- ---------------------------------------------------------------------------
-- 4. self_context gains visible_on_board (re-issues 0006:214-236).
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
  select jsonb_build_object('username', p.username_display,
                            'is_test', p.is_test,
                            'visible_on_board', p.visible_on_board)
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

-- ---------------------------------------------------------------------------
-- 5. Public predicates gain the visibility filter (re-issues 0009:47-108).
--    Append `and p.visible_on_board = true` to the membership predicate; the
--    SELECT lists, rank(), ordering and limits are unchanged, so the filter
--    applies before ranking. The 0006 test-cohort helpers are NOT edited.
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
        and p.visible_on_board = true
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
    and (p.is_test = false or p.is_demo = true)
    and p.visible_on_board = true;
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

-- Re-assert the public wrappers' grants (bodies unchanged; signatures intact).
revoke execute on function public.hiscores(text, int) from public;
grant execute on function public.hiscores(text, int) to anon, authenticated;
revoke execute on function public.public_profile(text) from public;
grant execute on function public.public_profile(text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- 6. Capability marker gains board_visibility (re-issues 0009:145-164).
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
    'board_visibility', true,
    'test_rpcs', jsonb_build_array('self_context','test_hiscores',
                                   'test_public_profile'),
    'operation_kinds', jsonb_build_array(
      'review_award','review_retract','review_restore','catchup_preset','review_skip'))
$$;
revoke all on function public.evolved_capabilities() from public, anon, authenticated;
grant execute on function public.evolved_capabilities() to anon, authenticated;
