-- 0014_linear_replay.sql - evolved_replay in linear time (issue #63).
-- Append-only migration: never edit 0001-0013 as a rollout strategy.
--
-- submit_operations calls evolved_replay on every upload, and replay walks
-- the game's entire operation history. The 0005 body was quadratic in that
-- history:
--   * winner selection looped over every award, and for each one re-scanned
--     every retract, every skip and every award (O(awards x ops));
--   * review_outcomes was grown with jsonb_set, copying the whole object on
--     every winner (O(winners^2) bytes);
--   * all six levels were recomputed through evolved_level (a non-inlinable
--     SQL function) before every winner.
-- At ~3,000 operations a submit ran past the authenticated role's 8 s
-- statement_timeout, PostgREST returned 57014, nothing was accepted, and the
-- client's pending queue grew with every review ("Sync unavailable -
-- progress saved"). Every new review made the next attempt slower.
--
-- This re-issues evolved_replay with the same signature, attributes, grants
-- and output. Output is unchanged byte for byte, including two existing
-- behaviors that differ from the Python reducer and are deliberately NOT
-- fixed here, because fixing them would re-score live players:
--   * a winner is emitted once per award row, not once per review key, so a
--     key with several awards is applied once per award;
--   * the catch-up preset is the last preset in canonical order whose
--     effective_ts <= review_ts (the reducer sorts presets by ts first).
-- What changed:
--   * claims are resolved in one scan with window functions (a sort, no
--     joins, so no plan depends on table statistics);
--   * review_outcomes is built once with jsonb_object_agg (last write wins,
--     the same as repeated jsonb_set);
--   * only the level of the skill whose XP changed is recomputed.
-- No table, column, grant, trigger, capability or client change.

create or replace function public.evolved_replay(p_game_uuid uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_rules jsonb := public.evolved_rules();
  v_thresholds bigint[];
  v_xp bigint[] := array[0,0,0,0,0,0];
  v_inv jsonb := '{}'::jsonb;
  v_success bigint := 0;
  v_attempts bigint := 0;
  v_cooks bigint := 0;
  v_gems bigint := 0;
  v_first_catch int := 0;
  v_first_cook int := 0;
  v_levels int[] := array[1,1,1,1,1,1];
  v_adjustments text[] := '{}';
  v_diagnostics text[];
  v_claim_diagnostics text[];
  v_out_keys text[] := '{}';
  v_out_vals jsonb[] := '{}';
  v_outcomes jsonb;
  v_pos bigint;
  v_preset_ts bigint[];
  v_preset_skill text[];
  v_bad record;
  v_win_rk text[];
  v_win_op jsonb[];
  v_i int;
  v_j int;
  v_skill text;
  v_skill_i int;
  v_policy int;
  v_res jsonb;
  v_consumed jsonb;
  v_key text;
  v_val_text text;
  v_val bigint;
  v_has_conflict boolean;
  v_conflict_key text;
  v_preset_skill_name text;
  v_ts bigint;
  v_achievements text[] := '{}';
begin
  select coalesce(array_agg((value)::bigint order by ord), '{}')
    into v_thresholds
    from jsonb_array_elements_text(v_rules->'thresholds') with ordinality as t(value, ord);

  -- An unsupported reward policy aborts the replay, as in 0005 (first one in
  -- canonical order names the op).
  select o.op_id::text as op_id,
         coalesce((o.payload->>'reward_policy')::int, 1) as policy
    into v_bad
    from public.game_operations o
    where o.game_uuid = p_game_uuid
      and o.kind = 'review_award'
      and coalesce(o.payload->>'review_key', '') <> ''
      and coalesce((o.payload->>'reward_policy')::int, 1) not in (1, 2)
    order by o.lamport, o.device_id, o.device_seq, o.op_id
    limit 1;
  if found then
    raise exception 'unsupported_policy:% op %', v_bad.policy, v_bad.op_id
      using errcode = '22000';
  end if;

  -- Collection-pass diagnostics in canonical order, the revision count, and
  -- the preset timeline (canonical order, valid presets only).
  with ops as (
    select kind, payload, op_id::text as op_id,
           row_number() over (order by lamport, device_id, device_seq, op_id) as pos
      from public.game_operations
     where game_uuid = p_game_uuid
  )
  select
    coalesce((select array_agg(d order by pos) from (
      select pos, case
          when kind = 'catchup_preset'
               and lower(coalesce(payload->>'skill', '')) not in
                   ('mining','woodcutting','fishing')
            then 'bad_preset_skill:' || op_id
          when kind in ('review_award', 'review_skip')
               and coalesce(payload->>'review_key', '') = ''
            then 'missing_review_key:' || op_id
          when kind in ('review_retract', 'review_restore')
               and coalesce(payload->>'target_review_key', '') = ''
            then 'missing_target_key:' || op_id
        end as d
        from ops) s where d is not null), '{}'),
    coalesce((select count(*) from ops), 0),
    coalesce((select array_agg(coalesce((payload->>'effective_ts')::bigint, 0)
                               order by pos)
                from ops where kind = 'catchup_preset'
                 and lower(coalesce(payload->>'skill', '')) in
                     ('mining','woodcutting','fishing')), '{}'),
    coalesce((select array_agg(lower(payload->>'skill') order by pos)
                from ops where kind = 'catchup_preset'
                 and lower(coalesce(payload->>'skill', '')) in
                     ('mining','woodcutting','fishing')), '{}')
    into v_diagnostics, v_pos, v_preset_ts, v_preset_skill;

  -- Claim resolution per award row (direct > skip > catch-up; the last
  -- retract/restore decides), in one scan: every claim is keyed by its review
  -- key and resolved with window functions, so the cost is a sort, with no
  -- join for the planner to turn into a nested loop on a table without
  -- statistics. claim_skipped is emitted once per award row of a skipped key,
  -- in award order, after the collection diagnostics. Winners are emitted
  -- once per award row, ordered by the winning claim's position.
  with ops as (
    select kind, payload,
           row_number() over (order by lamport, device_id, device_seq, op_id) as pos
      from public.game_operations
     where game_uuid = p_game_uuid
  ),
  keyed as (
    select pos, kind, payload,
           case when kind in ('review_retract', 'review_restore')
                then payload->>'target_review_key'
                else payload->>'review_key' end as rk,
           kind = 'review_award'
             and coalesce(payload->>'provenance', 'catchup') = 'direct' as is_direct
      from ops
     where kind in ('review_award', 'review_skip',
                    'review_retract', 'review_restore')
  ),
  claims as (
    select pos, kind, rk,
           max(pos) filter (where kind in ('review_retract', 'review_restore'))
             over by_key as last_toggle,
           max(pos) filter (where kind = 'review_retract') over by_key as last_retract,
           bool_or(kind = 'review_skip') over by_key as has_skip,
           bool_or(is_direct) over by_key as has_direct,
           first_value(pos) over pick as win_pos,
           first_value(payload) over pick as win_op
      from keyed
     where coalesce(rk, '') <> ''
    window by_key as (partition by rk),
           pick as (partition by rk order by
                      case when is_direct then 0
                           when kind = 'review_award' then 1
                           else 2 end, pos)
  ),
  resolved as (
    select pos, rk, win_pos, win_op,
           case when last_retract is not null and last_retract = last_toggle
                  then 'retracted'
                when has_direct then 'win'
                when has_skip then 'skipped'
                else 'win' end as outcome
      from claims
     where kind = 'review_award'
  )
  select
    coalesce(array_agg('claim_skipped:' || rk order by pos)
               filter (where outcome = 'skipped'), '{}'),
    coalesce(array_agg(rk order by win_pos) filter (where outcome = 'win'), '{}'),
    coalesce(array_agg(win_op order by win_pos) filter (where outcome = 'win'), '{}')
    into v_claim_diagnostics, v_win_rk, v_win_op
    from resolved;
  v_diagnostics := v_diagnostics || v_claim_diagnostics;

  for v_j in 1..6 loop
    v_levels[v_j] := public.evolved_level(v_xp[v_j], v_thresholds);
  end loop;

  -- Replay winners in canonical order of the winning claim.
  for v_i in 1..coalesce(array_length(v_win_rk, 1), 0)
  loop
    v_res := null;
    v_policy := coalesce((v_win_op[v_i]->>'reward_policy')::int, 1);
    if coalesce(v_win_op[v_i]->>'provenance', 'catchup') = 'direct' then
      v_skill := lower(coalesce(v_win_op[v_i]->>'skill', ''));
      v_res := public._evolved_apply_direct(
        v_rules, v_skill, coalesce(v_win_op[v_i]->>'resource', ''),
        v_levels, v_inv, p_game_uuid::text, v_win_rk[v_i], v_policy);
    else
      v_ts := coalesce((v_win_op[v_i]->>'review_ts')::bigint, 0);
      v_preset_skill_name := 'mining';
      for v_j in 1..coalesce(array_length(v_preset_ts, 1), 0) loop
        if v_preset_ts[v_j] <= v_ts then
          v_preset_skill_name := v_preset_skill[v_j];
        end if;
      end loop;
      v_res := public._evolved_apply_catchup(
        v_rules, v_preset_skill_name, v_levels, v_inv,
        p_game_uuid::text, v_win_rk[v_i], v_policy);
    end if;
    if v_res is null then
      continue;
    end if;

    -- Validate all ingredients before consuming any (policy-2 conflict: zero).
    v_consumed := coalesce(v_res->'consumed', '{}'::jsonb);
    v_has_conflict := false;
    v_conflict_key := null;
    for v_key, v_val_text in
      select key, value from jsonb_each_text(v_consumed)
    loop
      if coalesce((v_inv->>v_key)::bigint, 0) < v_val_text::bigint then
        v_has_conflict := true;
        v_conflict_key := v_key;
        exit;
      end if;
    end loop;
    if v_has_conflict then
      v_adjustments := v_adjustments ||
        ('material_conflict:' || v_win_rk[v_i] || ':' || coalesce(v_conflict_key, '?'));
      if v_policy = 2 then
        v_res := jsonb_build_object('skill', v_res->>'skill', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb,
          'outcome', 'paused_materials');
      else
        v_res := jsonb_build_object('skill', v_res->>'skill', 'xp_micro', 1000000,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb,
          'outcome', 'practice');
      end if;
    else
      v_skill_i := public._evolved_skill_idx(v_res->>'skill');
      if v_skill_i > 0 then
        v_xp[v_skill_i] := v_xp[v_skill_i] + coalesce((v_res->>'xp_micro')::bigint, 0);
        -- Only this skill's XP moved; keep v_levels equal to
        -- evolved_level(v_xp) for every skill without recomputing all six.
        v_levels[v_skill_i] := public.evolved_level(v_xp[v_skill_i], v_thresholds);
      end if;
      for v_key, v_val_text in
        select key, value from jsonb_each_text(v_consumed)
      loop
        v_inv := jsonb_set(v_inv, array[v_key],
          to_jsonb(coalesce((v_inv->>v_key)::bigint, 0) - v_val_text::bigint), true);
      end loop;
      if v_res ? 'item_out' and v_res->>'item_out' is not null then
        v_inv := jsonb_set(v_inv, array[v_res->>'item_out'],
          to_jsonb(coalesce((v_inv->>(v_res->>'item_out'))::bigint, 0)
                   + coalesce((v_res->>'item_qty')::bigint, 1)), true);
      end if;
      if v_res ? 'gem_out' and v_res->>'gem_out' is not null then
        v_inv := jsonb_set(v_inv, array[v_res->>'gem_out'],
          to_jsonb(coalesce((v_inv->>(v_res->>'gem_out'))::bigint, 0) + 1), true);
      end if;
    end if;

    v_success := v_success
      + coalesce((v_res->'counters'->>'successful_actions')::bigint, 0);
    v_attempts := v_attempts
      + coalesce((v_res->'counters'->>'cooking_attempts')::bigint, 0);
    v_cooks := v_cooks
      + coalesce((v_res->'counters'->>'successful_cooks')::bigint, 0);
    v_gems := v_gems
      + coalesce((v_res->'counters'->>'gems')::bigint, 0);
    if v_res->>'outcome' = 'success' and v_res->>'skill' = 'fishing'
       and v_first_catch = 0 then
      v_first_catch := 1;
    end if;
    if v_res->>'outcome' = 'success' and v_res->>'skill' = 'cooking'
       and v_first_cook = 0 then
      v_first_cook := 1;
    end if;
    v_out_keys := v_out_keys || v_win_rk[v_i];
    v_out_vals := v_out_vals || jsonb_build_object(
        'skill', v_res->>'skill',
        'xp_micro', coalesce((v_res->>'xp_micro')::bigint, 0),
        'rewarded', coalesce((v_res->>'xp_micro')::bigint, 0) > 0,
        'outcome', v_res->>'outcome');
  end loop;

  select coalesce(jsonb_object_agg(k, v order by o), '{}'::jsonb)
    into v_outcomes
    from unnest(v_out_keys, v_out_vals) with ordinality as u(k, v, o);

  for v_j in 1..6 loop
    v_levels[v_j] := public.evolved_level(v_xp[v_j], v_thresholds);
  end loop;

  if v_first_catch = 1 then
    v_achievements := array_append(v_achievements, 'first_catch');
  end if;
  if v_first_cook = 1 then
    v_achievements := array_append(v_achievements, 'first_cook');
  end if;
  for v_i in 1..6 loop
    v_skill := public._evolved_skill_name(v_i);
    foreach v_val in array array[10,30,60,99] loop
      if v_levels[v_i] >= v_val then
        v_achievements := array_append(v_achievements,
          'skill_' || v_val || '_' || v_skill);
      end if;
    end loop;
  end loop;
  if v_cooks >= 100 then
    v_achievements := array_append(v_achievements, 'cooks_100');
  end if;
  if v_cooks >= 1000 then
    v_achievements := array_append(v_achievements, 'cooks_1000');
  end if;

  return jsonb_build_object(
    'xp_micro', (select jsonb_object_agg(public._evolved_skill_name(g), v_xp[g]::text)
                 from generate_series(1, 6) as g),
    'levels', (select jsonb_object_agg(public._evolved_skill_name(g), v_levels[g])
               from generate_series(1, 6) as g),
    'inventory', v_inv,
    'counters', jsonb_build_object(
      'successful_actions', v_success, 'cooking_attempts', v_attempts,
      'successful_cooks', v_cooks, 'gems', v_gems,
      'first_catch', v_first_catch, 'first_cook', v_first_cook),
    'achievements', to_jsonb(v_achievements),
    'adjustments', to_jsonb(v_adjustments),
    'diagnostics', to_jsonb(v_diagnostics),
    'review_outcomes', v_outcomes,
    'revision', v_pos);
end;
$$;

revoke all on function public.evolved_replay(uuid) from anon, authenticated;
-- Explicitly remove the implicit PUBLIC execute: security-definer functions
-- must not be callable by roles that were never granted access.
revoke execute on function public.evolved_replay(uuid) from public;
