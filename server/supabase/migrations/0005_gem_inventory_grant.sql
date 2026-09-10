-- 0005_gem_inventory_grant.sql - Gem loot grant + cooking level gate.
--
-- Two replay corrections, mirrored exactly in evolved/reducer.py:
--
-- 1. Gems are loot. _evolved_apply_direct has always returned gem_out on a
--    successful gem roll and evolved_replay counted it, but the gem item was
--    never written to the inventory: bonus gem XP and the gems counter fired
--    while the Uncut gem never reached the Bank, making every jewelry recipe
--    (tiers 11-23 of Crafting) unreachable. evolved_replay now writes it.
--
-- 2. Cooking is level-gated like every other skill. The rules carry
--    cooking_level per fish, the UI locks those tiers and Training Home
--    pauses on a lost level, but _evolved_apply_direct only checked
--    materials, so a policy-2 review after a level loss still awarded full
--    XP. Policy 2 now returns paused_level; legacy policy 1 earns practice
--    XP and consumes nothing, matching Smithing/Crafting.
--
-- XP math is otherwise unchanged. Covered by dev/scoring_parity.py
-- (gem_bonus_multiplier, cooking_level_gate) and tests/test_reducer.py.
-- Hosted deployment is a separate, authorized step.

create or replace function public._evolved_apply_direct(
  p_rules jsonb, p_skill text, p_resource text, p_levels bigint[],
  p_inv jsonb, p_game text, p_rk text, p_policy int)
returns jsonb
language plpgsql
immutable
as $$
declare
  v_rules_version int := coalesce((p_rules->>'rules_version')::int, 1);
  v_spec jsonb;
  v_idx int := public._evolved_skill_idx(p_skill);
  v_level bigint;
  v_mult bigint;
  v_xp bigint := 0;
  v_req jsonb;
  v_mats_ok boolean;
  v_success boolean;
  v_gem jsonb;
  v_gem_xp bigint := 0;
  v_level_ok boolean;
  v_has boolean;
  v_burned boolean;
  v_fish jsonb;
  v_base_xp jsonb;
begin
  if v_idx = 0 then
    if p_policy = 2 then
      return jsonb_build_object('skill', p_skill, 'xp_micro', 0,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
    end if;
    return jsonb_build_object('skill', 'mining', 'xp_micro', 1000000,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
  end if;
  v_level := p_levels[v_idx];

  if p_skill = 'mining' then
    v_spec := public._evolved_find(p_rules, 'ores', p_resource);
    if v_spec is null then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'mining', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
      end if;
      return jsonb_build_object('skill', 'mining', 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
    if v_level < (v_spec->>'level')::bigint then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'mining', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_level');
      end if;
      v_spec := public._evolved_first_unlocked(p_rules, 'ores', 'level', v_level::int);
    end if;
    v_success := public._evolved_action_hit(v_rules_version, v_level::int,
                                            v_spec->'probability', p_game, p_rk);
    v_mult := public._evolved_mult_micro(
      public._evolved_base_micro(v_spec->'base_xp'), (v_spec->>'tier')::int);
    if v_success then
      v_gem := null;
      if public._evolved_frac_hit(
          public._evolved_draw(v_rules_version, p_game, p_rk, 'gem_drop'), 1, 256) then
        v_gem := public._evolved_gem_pick(
          p_rules, public._evolved_draw(v_rules_version, p_game, p_rk, 'gem_pick'));
      end if;
      v_xp := v_mult;
      if v_gem is not null then
        v_gem_xp := public._evolved_mult_micro(
          public._evolved_base_micro(v_gem->'base_xp'), (v_spec->>'tier')::int);
        v_xp := v_xp + v_gem_xp;
      end if;
      return jsonb_build_object('skill', 'mining', 'xp_micro', v_xp,
        'consumed', '{}'::jsonb,
        'item_out', v_spec->>'display', 'item_qty', 1,
        'gem_out', case when v_gem is null then null else v_gem->>'display' end,
        'counters', case when v_gem is null
          then jsonb_build_object('successful_actions', 1)
          else jsonb_build_object('successful_actions', 1, 'gems', 1) end,
        'outcome', 'success');
    end if;
    v_xp := greatest(public._evolved_mul_half_up(v_mult, 1, 4), 1000000);
    return jsonb_build_object('skill', 'mining', 'xp_micro', v_xp,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'fail_gather');
  end if;

  if p_skill = 'woodcutting' then
    v_spec := public._evolved_find(p_rules, 'trees', p_resource);
    if v_spec is null then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'woodcutting', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
      end if;
      v_spec := public._evolved_first_unlocked(p_rules, 'trees', 'level', v_level::int);
    elsif v_level < (v_spec->>'level')::bigint then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'woodcutting', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_level');
      end if;
      v_spec := public._evolved_first_unlocked(p_rules, 'trees', 'level', v_level::int);
    end if;
    v_success := public._evolved_action_hit(v_rules_version, v_level::int,
                                            v_spec->'probability', p_game, p_rk);
    v_mult := public._evolved_mult_micro(
      public._evolved_base_micro(v_spec->'base_xp'), (v_spec->>'tier')::int);
    if v_success then
      return jsonb_build_object('skill', 'woodcutting', 'xp_micro', v_mult,
        'consumed', '{}'::jsonb, 'item_out', v_spec->>'display', 'item_qty', 1,
        'counters', jsonb_build_object('successful_actions', 1),
        'outcome', 'success');
    end if;
    v_xp := greatest(public._evolved_mul_half_up(v_mult, 1, 4), 1000000);
    return jsonb_build_object('skill', 'woodcutting', 'xp_micro', v_xp,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'fail_gather');
  end if;

  if p_skill = 'fishing' then
    v_spec := public._evolved_find(p_rules, 'fish', p_resource);
    if v_spec is null then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'fishing', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
      end if;
      v_spec := public._evolved_first_unlocked(p_rules, 'fish', 'fishing_level', v_level::int);
    elsif v_level < (v_spec->>'fishing_level')::bigint then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'fishing', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_level');
      end if;
      v_spec := public._evolved_first_unlocked(p_rules, 'fish', 'fishing_level', v_level::int);
    end if;
    v_success := public._evolved_action_hit(v_rules_version, v_level::int,
                                            v_spec->'probability', p_game, p_rk);
    v_mult := public._evolved_mult_micro(
      public._evolved_base_micro(v_spec->'fishing_base_xp'), (v_spec->>'tier')::int);
    if v_success then
      return jsonb_build_object('skill', 'fishing', 'xp_micro', v_mult,
        'consumed', '{}'::jsonb, 'item_out', v_spec->>'display', 'item_qty', 1,
        'counters', jsonb_build_object('successful_actions', 1),
        'outcome', 'success');
    end if;
    v_xp := greatest(public._evolved_mul_half_up(v_mult, 1, 4), 1000000);
    return jsonb_build_object('skill', 'fishing', 'xp_micro', v_xp,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'fail_gather');
  end if;

  if p_skill = 'cooking' then
    v_fish := public._evolved_find(p_rules, 'fish', p_resource);
    if v_fish is null then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'cooking', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
      end if;
      return jsonb_build_object('skill', 'cooking', 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
    v_level_ok := v_level >= (v_fish->>'cooking_level')::bigint;
    if not v_level_ok then
      if p_policy = 2 then
        return jsonb_build_object('skill', 'cooking', 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_level');
      end if;
      return jsonb_build_object('skill', 'cooking', 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
    v_has := coalesce((p_inv->>p_resource)::bigint, 0) >= 1;
    if p_policy = 2 and not v_has then
      return jsonb_build_object('skill', 'cooking', 'xp_micro', 0,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_materials');
    end if;
    v_burned := public._evolved_frac_hit(
      public._evolved_draw(v_rules_version, p_game, p_rk, 'burn'),
      greatest(300 - 2 * v_level::int, 0)::bigint, 1000::bigint);
    v_mult := public._evolved_mult_micro(
      public._evolved_base_micro(v_fish->'cooking_base_xp'), (v_fish->>'tier')::int);
    if not v_has then
      return jsonb_build_object('skill', 'cooking', 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
    if v_burned then
      v_xp := greatest(public._evolved_mul_half_up(v_mult, 1, 4), 1000000);
      return jsonb_build_object('skill', 'cooking', 'xp_micro', v_xp,
        'consumed', jsonb_build_object(p_resource, 1),
        'counters', jsonb_build_object('cooking_attempts', 1),
        'outcome', 'burn');
    end if;
    return jsonb_build_object('skill', 'cooking', 'xp_micro', v_mult,
      'consumed', jsonb_build_object(p_resource, 1),
      'item_out', 'Cooked ' || p_resource, 'item_qty', 1,
      'counters', jsonb_build_object('successful_actions', 1,
        'cooking_attempts', 1, 'successful_cooks', 1),
      'outcome', 'success');
  end if;

  if p_skill in ('smithing', 'crafting') then
    v_spec := public._evolved_find(p_rules,
      case when p_skill = 'smithing' then 'bars' else 'crafting' end, p_resource);
    if v_spec is null then
      if p_policy = 2 then
        return jsonb_build_object('skill', p_skill, 'xp_micro', 0,
          'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
      end if;
      return jsonb_build_object('skill', p_skill, 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
    v_req := coalesce(v_spec->'ore_required', v_spec->'requirements', '{}'::jsonb);
    v_level_ok := v_level >= (v_spec->>'level')::bigint;
    select coalesce(bool_and(coalesce((p_inv->>key)::bigint, 0) >= (value)::bigint), true)
      into v_mats_ok
      from jsonb_each_text(v_req);
    if p_policy = 2 and not v_level_ok then
      return jsonb_build_object('skill', p_skill, 'xp_micro', 0,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_level');
    end if;
    if p_policy = 2 and not v_mats_ok then
      return jsonb_build_object('skill', p_skill, 'xp_micro', 0,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_materials');
    end if;
    if v_level_ok and v_mats_ok then
      v_mult := public._evolved_mult_micro(
        public._evolved_base_micro(v_spec->'base_xp'), (v_spec->>'tier')::int);
      return jsonb_build_object('skill', p_skill, 'xp_micro', v_mult,
        'consumed', v_req, 'item_out', v_spec->>'display', 'item_qty', 1,
        'counters', jsonb_build_object('successful_actions', 1),
        'outcome', 'success');
    end if;
    return jsonb_build_object('skill', p_skill, 'xp_micro', 1000000,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
  end if;

  if p_policy = 2 then
    return jsonb_build_object('skill', 'mining', 'xp_micro', 0,
      'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'invalid');
  end if;
  return jsonb_build_object('skill', 'mining', 'xp_micro', 1000000,
    'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
end;
$$;

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
  v_diagnostics text[] := '{}';
  v_outcomes jsonb := '{}'::jsonb;
  v_pos bigint := 0;
  v_award_rk text[] := '{}';
  v_award_prov text[] := '{}';
  v_award_op jsonb[] := '{}';
  v_award_pos bigint[] := '{}';
  v_retract_rk text[] := '{}';
  v_retract_kind text[] := '{}';
  v_retract_pos bigint[] := '{}';
  v_skip_rk text[] := '{}';
  v_preset_ts bigint[] := '{}';
  v_preset_skill text[] := '{}';
  v_win_rk text[] := '{}';
  v_win_op jsonb[] := '{}';
  v_win_pos bigint[] := '{}';
  v_sorted_rk text[];
  v_sorted_op jsonb[];
  v_i int;
  v_j int;
  v_rk text;
  v_skill text;
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
  v_op record;
begin
  select coalesce(array_agg((value)::bigint order by ord), '{}')
    into v_thresholds
    from jsonb_array_elements_text(v_rules->'thresholds') with ordinality as t(value, ord);

  -- Collect claims in canonical order.
  for v_op in
    select kind, payload, op_id::text as op_id
    from public.game_operations
    where game_uuid = p_game_uuid
    order by lamport, device_id, device_seq, op_id
  loop
    v_pos := v_pos + 1;
    if v_op.kind = 'catchup_preset' then
      if lower(coalesce(v_op.payload->>'skill', '')) not in
         ('mining','woodcutting','fishing') then
        v_diagnostics := v_diagnostics || ('bad_preset_skill:' || v_op.op_id);
      else
        v_preset_ts := v_preset_ts
          || coalesce((v_op.payload->>'effective_ts')::bigint, 0);
        v_preset_skill := v_preset_skill || lower(v_op.payload->>'skill');
      end if;
    elsif v_op.kind = 'review_award' then
      if coalesce(v_op.payload->>'review_key', '') = '' then
        v_diagnostics := v_diagnostics || ('missing_review_key:' || v_op.op_id);
      else
        v_policy := coalesce((v_op.payload->>'reward_policy')::int, 1);
        if v_policy not in (1, 2) then
          raise exception 'unsupported_policy:% op %', v_policy, v_op.op_id
            using errcode = '22000';
        end if;
        v_award_rk := v_award_rk || (v_op.payload->>'review_key');
        v_award_prov := v_award_prov
          || coalesce(v_op.payload->>'provenance', 'catchup');
        v_award_op := v_award_op || v_op.payload;
        v_award_pos := v_award_pos || v_pos;
      end if;
    elsif v_op.kind in ('review_retract', 'review_restore') then
      if coalesce(v_op.payload->>'target_review_key', '') = '' then
        v_diagnostics := v_diagnostics || ('missing_target_key:' || v_op.op_id);
      else
        v_retract_rk := v_retract_rk || (v_op.payload->>'target_review_key');
        v_retract_kind := v_retract_kind || v_op.kind;
        v_retract_pos := v_retract_pos || v_pos;
      end if;
    elsif v_op.kind = 'review_skip' then
      if coalesce(v_op.payload->>'review_key', '') = '' then
        v_diagnostics := v_diagnostics || ('missing_review_key:' || v_op.op_id);
      else
        v_skip_rk := v_skip_rk || (v_op.payload->>'review_key');
      end if;
    end if;
  end loop;

  -- Winning claim per review_key: direct > skip > catch-up; last retract wins.
  for v_i in 1..coalesce(array_length(v_award_rk, 1), 0) loop
    v_rk := v_award_rk[v_i];
    declare
      v_last_kind text := null;
      v_last_pos bigint := 0;
      v_direct_pos bigint := null;
      v_direct_op jsonb;
      v_catch_pos bigint := null;
      v_catch_op jsonb;
      v_has_skip boolean := false;
    begin
      for v_j in 1..coalesce(array_length(v_retract_rk, 1), 0) loop
        if v_retract_rk[v_j] = v_rk and v_retract_pos[v_j] > v_last_pos then
          v_last_pos := v_retract_pos[v_j];
          v_last_kind := v_retract_kind[v_j];
        end if;
      end loop;
      if v_last_kind = 'review_retract' then
        continue;
      end if;
      for v_j in 1..coalesce(array_length(v_skip_rk, 1), 0) loop
        if v_skip_rk[v_j] = v_rk then
          v_has_skip := true;
          exit;
        end if;
      end loop;
      for v_j in 1..array_length(v_award_rk, 1) loop
        if v_award_rk[v_j] <> v_rk then
          continue;
        end if;
        if v_award_prov[v_j] = 'direct' then
          if v_direct_pos is null or v_award_pos[v_j] < v_direct_pos then
            v_direct_pos := v_award_pos[v_j];
            v_direct_op := v_award_op[v_j];
          end if;
        else
          if v_catch_pos is null or v_award_pos[v_j] < v_catch_pos then
            v_catch_pos := v_award_pos[v_j];
            v_catch_op := v_award_op[v_j];
          end if;
        end if;
      end loop;
      if v_direct_pos is not null then
        v_win_rk := v_win_rk || v_rk;
        v_win_op := v_win_op || v_direct_op;
        v_win_pos := v_win_pos || v_direct_pos;
      elsif v_has_skip then
        v_diagnostics := v_diagnostics || ('claim_skipped:' || v_rk);
      elsif v_catch_pos is not null then
        v_win_rk := v_win_rk || v_rk;
        v_win_op := v_win_op || v_catch_op;
        v_win_pos := v_win_pos || v_catch_pos;
      end if;
    end;
  end loop;

  if coalesce(array_length(v_win_rk, 1), 0) > 0 then
    select array_agg(rk order by pos), array_agg(op order by pos)
      into v_sorted_rk, v_sorted_op
      from unnest(v_win_rk, v_win_op, v_win_pos) as t(rk, op, pos);
  else
    v_sorted_rk := '{}';
    v_sorted_op := '{}';
  end if;

  -- Replay winners.
  for v_i in 1..coalesce(array_length(v_sorted_rk, 1), 0) loop
    v_key := v_sorted_rk[v_i];
    v_res := null;
    v_policy := coalesce((v_sorted_op[v_i]->>'reward_policy')::int, 1);
    for v_j in 1..6 loop
      v_levels[v_j] := public.evolved_level(v_xp[v_j], v_thresholds);
    end loop;
    if coalesce(v_sorted_op[v_i]->>'provenance', 'catchup') = 'direct' then
      v_skill := lower(coalesce(v_sorted_op[v_i]->>'skill', ''));
      v_res := public._evolved_apply_direct(
        v_rules, v_skill, coalesce(v_sorted_op[v_i]->>'resource', ''),
        v_levels, v_inv, p_game_uuid::text, v_key, v_policy);
    else
      v_ts := coalesce((v_sorted_op[v_i]->>'review_ts')::bigint, 0);
      v_preset_skill_name := 'mining';
      for v_j in 1..coalesce(array_length(v_preset_ts, 1), 0) loop
        if v_preset_ts[v_j] <= v_ts then
          v_preset_skill_name := v_preset_skill[v_j];
        end if;
      end loop;
      v_res := public._evolved_apply_catchup(
        v_rules, v_preset_skill_name, v_levels, v_inv,
        p_game_uuid::text, v_key, v_policy);
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
        ('material_conflict:' || v_sorted_rk[v_i] || ':' || coalesce(v_conflict_key, '?'));
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
      v_i := v_i;  -- keep loop var untouched for the apply below
      declare
        v_skill_i int := public._evolved_skill_idx(v_res->>'skill');
      begin
        if v_skill_i > 0 then
          v_xp[v_skill_i] := v_xp[v_skill_i] + coalesce((v_res->>'xp_micro')::bigint, 0);
        end if;
      end;
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
    v_outcomes := jsonb_set(v_outcomes, array[v_sorted_rk[v_i]],
      jsonb_build_object(
        'skill', v_res->>'skill',
        'xp_micro', coalesce((v_res->>'xp_micro')::bigint, 0),
        'rewarded', coalesce((v_res->>'xp_micro')::bigint, 0) > 0,
        'outcome', v_res->>'outcome'), true);
  end loop;

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
