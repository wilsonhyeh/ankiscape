-- 0013_overall_hiscores.sql - Overall board on the public Hiscores.
-- Append-only migration: never edit 0001-0012 as a rollout strategy.
--
-- public.hiscores(p_skill, p_limit) has ranked one skill at a time and raised
-- bad_skill for anything else. This re-issues the private ranking helper
-- (0011's _hiscores_public) so p_skill = 'overall' ranks players by total XP
-- across the six skills.
--
-- Deliberately narrow:
--   * Read-only. No table, column, grant, trigger or capability changes; the
--     public wrapper hiscores(text, int) and its grants are untouched.
--   * Same row shape {rank, username, xp, is_demo}, same allowlist, same
--     membership predicate (active, has a game, real or demo, visible on the
--     board) and the same filter-before-rank behavior as 0011. Only the value
--     being ranked changes for 'overall'.
--   * Every existing skill still ranks exactly as before; an unknown skill
--     still raises bad_skill (errcode 22000).
--   * Ranking is by total XP, not total level: level is derived client-side
--     from the shared rules table, and duplicating that table in SQL would
--     create a second source of truth that scoring parity does not cover.
--   * The test cohort (0006 test_hiscores) is not changed.

create or replace function public._hiscores_public(p_skill text, p_limit int)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_lim int := least(greatest(coalesce(p_limit, 50), 1), 100);
  v_rows jsonb;
begin
  if p_skill not in ('overall','mining','woodcutting','smithing','crafting',
                     'fishing','cooking') then
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
             v.xp,
             rank() over (order by v.xp desc) as rank
      from public.players p
      join public.game_state s on s.game_uuid = p.game_uuid
      cross join lateral (
        select case when p_skill = 'overall' then
                 coalesce((s.xp ->> 'mining')::bigint, 0)
               + coalesce((s.xp ->> 'woodcutting')::bigint, 0)
               + coalesce((s.xp ->> 'smithing')::bigint, 0)
               + coalesce((s.xp ->> 'crafting')::bigint, 0)
               + coalesce((s.xp ->> 'fishing')::bigint, 0)
               + coalesce((s.xp ->> 'cooking')::bigint, 0)
               else coalesce((s.xp ->> p_skill)::bigint, 0)
             end as xp) v
      where p.status = 'active' and p.game_uuid is not null
        and (p.is_test = false or p.is_demo = true)
        and p.visible_on_board = true
      order by v.xp desc, p.username_norm, p.user_id
      limit v_lim) t;
  return v_rows;
end;
$$;
revoke all on function public._hiscores_public(text, int)
  from public, anon, authenticated;
