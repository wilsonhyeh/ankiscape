-- 0016_aligned_replay.sql - server scoring matches the Python reducer, and
-- one apply step shared by every scoring path.
-- Append-only migration: never edit 0001-0015 as a rollout strategy.
-- Re-runnable: dev/scoring_parity.py re-applies it directly.
--
-- 1. Claims resolve per review key, exactly as evolved/reducer.py
--    _select_claims does: the earliest direct award wins; otherwise a skip
--    claim suppresses the key; otherwise the earliest catch-up award wins; a
--    key whose last retract/restore is a retract is disabled. 0014 emitted a
--    winner once per award row, so a key with several awards was applied
--    several times (production had 0 such keys on 2026-10-08).
-- 2. Catch-up presets are sorted by (effective_ts, canonical position), as
--    the reducer's _preset_timeline does; 0014 took them in canonical order
--    (production had 0 presets on 2026-10-08).
-- 3. Diagnostics: duplicate_direct_ignored / duplicate_catchup_ignored /
--    claim_skipped once per key, in order of the key's first award.
-- 4. The per-winner apply step is one function, _evolved_apply_winner, over
--    one state type, evolved_fold_state. evolved_replay calls it, and 0017's
--    incremental fold calls the same function, so the paths cannot drift.
--
-- For histories with no duplicate keys and no presets the output of
-- evolved_replay is byte-identical to 0014's.

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'public' and t.typname = 'evolved_fold_state') then
    create type public.evolved_fold_state as (
      xp bigint[],          -- micro-XP per skill, _evolved_skill_idx order
      levels int[],         -- evolved_level(xp[i]) per skill, kept in step
      inv jsonb,
      success bigint,
      attempts bigint,
      cooks bigint,
      gems bigint,
      first_catch int,
      first_cook int,
      adjustments text[]);
  end if;
end;
$$;

create or replace function public._evolved_thresholds(p_rules jsonb)
returns bigint[]
language sql
immutable
set search_path = public
as $$
  select coalesce(array_agg((value)::bigint order by ord), '{}')
    from jsonb_array_elements_text(p_rules->'thresholds') with ordinality as t(value, ord)
$$;

create or replace function public._evolved_fold_empty(p_thresholds bigint[])
returns public.evolved_fold_state
language sql
immutable
set search_path = public
as $$
  select row(array[0,0,0,0,0,0]::bigint[],
             (select array_agg(public.evolved_level(0, p_thresholds)) from generate_series(1, 6)),
             '{}'::jsonb, 0, 0, 0, 0, 0, 0, '{}'::text[])::public.evolved_fold_state
$$;

-- Last preset (arrays pre-sorted by effective_ts, canonical) with ts <= review_ts.
create or replace function public._evolved_preset_skill(
  p_ts bigint[], p_skill text[], p_review_ts bigint)
returns text
language plpgsql
immutable
as $$
declare
  v_name text := 'mining';
  v_j int;
begin
  for v_j in 1..coalesce(array_length(p_ts, 1), 0) loop
    exit when p_ts[v_j] > p_review_ts;
    v_name := p_skill[v_j];
  end loop;
  return v_name;
end;
$$;

-- The game's valid presets, sorted like the reducer's _preset_timeline.
create or replace function public._evolved_presets(
  p_game uuid, out o_ts bigint[], out o_skill text[])
language sql
stable
set search_path = public
as $$
  select coalesce(array_agg(coalesce((payload->>'effective_ts')::bigint, 0)
                            order by coalesce((payload->>'effective_ts')::bigint, 0),
                                     lamport, device_id, device_seq, op_id), '{}'),
         coalesce(array_agg(lower(payload->>'skill')
                            order by coalesce((payload->>'effective_ts')::bigint, 0),
                                     lamport, device_id, device_seq, op_id), '{}')
    from public.game_operations
   where game_uuid = p_game and kind = 'catchup_preset'
     and lower(coalesce(payload->>'skill', '')) in ('mining','woodcutting','fishing')
$$;

-- Apply one winning claim (the 0014 loop body, unchanged in substance).
create or replace function public._evolved_apply_winner(
  p_rules jsonb, p_thresholds bigint[], p_state public.evolved_fold_state,
  p_game text, p_rk text, p_op jsonb, p_preset_skill text,
  out o_state public.evolved_fold_state, out o_outcome jsonb)
language plpgsql
immutable
set search_path = public
as $$
declare
  v_policy int := coalesce((p_op->>'reward_policy')::int, 1);
  v_res jsonb;
  v_consumed jsonb;
  v_inv jsonb := p_state.inv;
  v_xp bigint[] := p_state.xp;
  v_levels int[] := p_state.levels;
  v_adjustments text[] := p_state.adjustments;
  v_key text;
  v_val_text text;
  v_has_conflict boolean := false;
  v_conflict_key text;
  v_skill_i int;
begin
  o_state := p_state;
  o_outcome := null;
  if coalesce(p_op->>'provenance', 'catchup') = 'direct' then
    v_res := public._evolved_apply_direct(
      p_rules, lower(coalesce(p_op->>'skill', '')), coalesce(p_op->>'resource', ''),
      v_levels, v_inv, p_game, p_rk, v_policy);
  else
    v_res := public._evolved_apply_catchup(
      p_rules, coalesce(p_preset_skill, 'mining'), v_levels, v_inv, p_game, p_rk, v_policy);
  end if;
  if v_res is null then
    return;
  end if;

  -- Validate all ingredients before consuming any (policy-2 conflict: zero).
  v_consumed := coalesce(v_res->'consumed', '{}'::jsonb);
  for v_key, v_val_text in select key, value from jsonb_each_text(v_consumed) loop
    if coalesce((v_inv->>v_key)::bigint, 0) < v_val_text::bigint then
      v_has_conflict := true;
      v_conflict_key := v_key;
      exit;
    end if;
  end loop;
  if v_has_conflict then
    v_adjustments := v_adjustments ||
      ('material_conflict:' || p_rk || ':' || coalesce(v_conflict_key, '?'));
    if v_policy = 2 then
      v_res := jsonb_build_object('skill', v_res->>'skill', 'xp_micro', 0,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'paused_materials');
    else
      v_res := jsonb_build_object('skill', v_res->>'skill', 'xp_micro', 1000000,
        'consumed', '{}'::jsonb, 'counters', '{}'::jsonb, 'outcome', 'practice');
    end if;
  else
    v_skill_i := public._evolved_skill_idx(v_res->>'skill');
    if v_skill_i > 0 then
      v_xp[v_skill_i] := v_xp[v_skill_i] + coalesce((v_res->>'xp_micro')::bigint, 0);
      v_levels[v_skill_i] := public.evolved_level(v_xp[v_skill_i], p_thresholds);
    end if;
    for v_key, v_val_text in select key, value from jsonb_each_text(v_consumed) loop
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

  o_state.xp := v_xp;
  o_state.levels := v_levels;
  o_state.inv := v_inv;
  o_state.adjustments := v_adjustments;
  o_state.success := p_state.success
    + coalesce((v_res->'counters'->>'successful_actions')::bigint, 0);
  o_state.attempts := p_state.attempts
    + coalesce((v_res->'counters'->>'cooking_attempts')::bigint, 0);
  o_state.cooks := p_state.cooks
    + coalesce((v_res->'counters'->>'successful_cooks')::bigint, 0);
  o_state.gems := p_state.gems
    + coalesce((v_res->'counters'->>'gems')::bigint, 0);
  if v_res->>'outcome' = 'success' and v_res->>'skill' = 'fishing'
     and p_state.first_catch = 0 then
    o_state.first_catch := 1;
  end if;
  if v_res->>'outcome' = 'success' and v_res->>'skill' = 'cooking'
     and p_state.first_cook = 0 then
    o_state.first_cook := 1;
  end if;
  o_outcome := jsonb_build_object(
    'skill', v_res->>'skill',
    'xp_micro', coalesce((v_res->>'xp_micro')::bigint, 0),
    'rewarded', coalesce((v_res->>'xp_micro')::bigint, 0) > 0,
    'outcome', v_res->>'outcome');
end;
$$;

-- xp/levels/inventory/counters/achievements of a fold state, shaped exactly
-- like evolved_replay's output (and game_state's columns).
create or replace function public._evolved_state_json(p_state public.evolved_fold_state)
returns jsonb
language plpgsql
immutable
set search_path = public
as $$
declare
  v_achievements text[] := '{}';
  v_i int;
  v_val int;
begin
  if p_state.first_catch = 1 then
    v_achievements := array_append(v_achievements, 'first_catch');
  end if;
  if p_state.first_cook = 1 then
    v_achievements := array_append(v_achievements, 'first_cook');
  end if;
  for v_i in 1..6 loop
    foreach v_val in array array[10,30,60,99] loop
      if p_state.levels[v_i] >= v_val then
        v_achievements := array_append(v_achievements,
          'skill_' || v_val || '_' || public._evolved_skill_name(v_i));
      end if;
    end loop;
  end loop;
  if p_state.cooks >= 100 then
    v_achievements := array_append(v_achievements, 'cooks_100');
  end if;
  if p_state.cooks >= 1000 then
    v_achievements := array_append(v_achievements, 'cooks_1000');
  end if;
  return jsonb_build_object(
    'xp_micro', (select jsonb_object_agg(public._evolved_skill_name(g), p_state.xp[g]::text)
                 from generate_series(1, 6) as g),
    'levels', (select jsonb_object_agg(public._evolved_skill_name(g), p_state.levels[g])
               from generate_series(1, 6) as g),
    'inventory', p_state.inv,
    'counters', jsonb_build_object(
      'successful_actions', p_state.success, 'cooking_attempts', p_state.attempts,
      'successful_cooks', p_state.cooks, 'gems', p_state.gems,
      'first_catch', p_state.first_catch, 'first_cook', p_state.first_cook),
    'achievements', to_jsonb(v_achievements));
end;
$$;

-- Collection-pass diagnostics (malformed operations), canonical order.
create or replace function public._evolved_collection_diagnostics(p_game uuid)
returns text[]
language sql
stable
set search_path = public
as $$
  select coalesce(array_agg(d order by lamport, device_id, device_seq, op_id), '{}')
    from (
      select lamport, device_id, device_seq, op_id, case
          when kind = 'catchup_preset'
               and lower(coalesce(payload->>'skill', '')) not in
                   ('mining','woodcutting','fishing')
            then 'bad_preset_skill:' || op_id::text
          when kind in ('review_award', 'review_skip')
               and coalesce(payload->>'review_key', '') = ''
            then 'missing_review_key:' || op_id::text
          when kind in ('review_retract', 'review_restore')
               and coalesce(payload->>'target_review_key', '') = ''
            then 'missing_target_key:' || op_id::text
        end as d
        from public.game_operations
       where game_uuid = p_game) s
   where d is not null
$$;

-- Per-key claim resolution in one scan (window functions + DISTINCT ON: sorts
-- only, no joins, so no plan depends on table statistics).
--   status: active    - has a winner that scores
--           retracted - last retract/restore is a retract (winner kept for a
--                       later restore; null when a skip would suppress it)
--           skipped   - a skip claim suppresses it (no direct award)
--           none      - only retract/restore operations name it
--   simple: exactly one award and no skip claim. Undo/redo of a key that is
--           not simple changes its diagnostics, so 0017's fast path rebuilds.
create or replace function public._evolved_resolve_claims(p_game uuid)
returns table(review_key text, status text, provenance text, winner_op uuid,
              w_lamport bigint, w_device text, w_seq bigint, win_pos bigint,
              win_payload jsonb, first_pos bigint, diag text, simple boolean)
language sql
stable
set search_path = public
as $$
  with ops as (
    select kind, payload, op_id, lamport, device_id, device_seq,
           row_number() over (order by lamport, device_id, device_seq, op_id) as pos
      from public.game_operations
     where game_uuid = p_game
  ),
  keyed as (
    select pos, kind, payload, op_id, lamport, device_id, device_seq,
           case when kind in ('review_retract', 'review_restore')
                then payload->>'target_review_key'
                else payload->>'review_key' end as rk,
           kind = 'review_award'
             and coalesce(payload->>'provenance', 'catchup') = 'direct' as is_direct,
           kind = 'review_award'
             and coalesce(payload->>'provenance', 'catchup') <> 'direct' as is_catch
      from ops
     where kind in ('review_award', 'review_skip', 'review_retract', 'review_restore')
  ),
  w as (
    select rk, pos,
           count(*) filter (where is_direct) over k as n_direct,
           count(*) filter (where is_catch) over k as n_catch,
           bool_or(kind = 'review_skip') over k as has_skip,
           max(pos) filter (where kind in ('review_retract', 'review_restore')) over k as last_toggle,
           max(pos) filter (where kind = 'review_retract') over k as last_retract,
           min(pos) filter (where kind = 'review_award') over k as first_award,
           first_value(pos) over pick as p_pos,
           first_value(payload) over pick as p_payload,
           first_value(op_id) over pick as p_op,
           first_value(lamport) over pick as p_lamport,
           first_value(device_id) over pick as p_device,
           first_value(device_seq) over pick as p_seq,
           first_value(is_direct) over pick as p_direct,
           first_value(is_catch) over pick as p_catch
      from keyed
     where coalesce(rk, '') <> ''
    window k as (partition by rk),
           pick as (partition by rk order by
                      case when is_direct then 0 when is_catch then 1 else 2 end, pos
                    rows between unbounded preceding and unbounded following)
  ),
  per_key as (
    select distinct on (rk) * from w order by rk, pos
  ),
  resolved as (
    select rk, first_award, p_pos, p_payload, p_op, p_lamport, p_device, p_seq,
           p_direct, n_direct, n_catch,
           case
             when first_award is null then case when has_skip then 'skipped' else 'none' end
             when last_retract is not null and last_retract = last_toggle then 'retracted'
             when n_direct > 0 then 'active'
             when has_skip then 'skipped'
             else 'active'
           end as status,
           -- The claim that wins (or would win after a restore).
           (first_award is not null and (n_direct > 0 or not has_skip)) as has_winner,
           case
             when first_award is null then null
             when last_retract is not null and last_retract = last_toggle then null
             when n_direct > 1 then 'duplicate_direct_ignored:' || rk
             when n_direct > 0 then null
             when has_skip then 'claim_skipped:' || rk
             when n_catch > 1 then 'duplicate_catchup_ignored:' || rk
           end as diag,
           (n_direct + n_catch = 1 and not has_skip) as simple
      from per_key
  )
  select rk, status,
         case when has_winner then case when p_direct then 'direct' else 'catchup' end end,
         case when has_winner then p_op end,
         case when has_winner then p_lamport end,
         case when has_winner then p_device end,
         case when has_winner then p_seq end,
         case when has_winner then p_pos end,
         case when has_winner then p_payload end,
         first_award,
         diag,
         simple
    from resolved
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
  v_thresholds bigint[] := public._evolved_thresholds(v_rules);
  v_state public.evolved_fold_state := public._evolved_fold_empty(v_thresholds);
  v_outcome jsonb;
  v_applied record;
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
  v_skill text;
begin
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

  v_diagnostics := public._evolved_collection_diagnostics(p_game_uuid);
  select count(*) into v_pos from public.game_operations where game_uuid = p_game_uuid;
  select o_ts, o_skill into v_preset_ts, v_preset_skill
    from public._evolved_presets(p_game_uuid);

  select coalesce(array_agg(c.diag order by c.first_pos) filter (where c.diag is not null), '{}'),
         coalesce(array_agg(c.review_key order by c.win_pos)
                    filter (where c.status = 'active'), '{}'),
         coalesce(array_agg(c.win_payload order by c.win_pos)
                    filter (where c.status = 'active'), '{}')
    into v_claim_diagnostics, v_win_rk, v_win_op
    from public._evolved_resolve_claims(p_game_uuid) c;
  v_diagnostics := v_diagnostics || v_claim_diagnostics;

  -- Replay winners in canonical order of the winning claim.
  for v_i in 1..coalesce(array_length(v_win_rk, 1), 0) loop
    v_skill := null;
    if coalesce(v_win_op[v_i]->>'provenance', 'catchup') <> 'direct' then
      v_skill := public._evolved_preset_skill(
        v_preset_ts, v_preset_skill, coalesce((v_win_op[v_i]->>'review_ts')::bigint, 0));
    end if;
    v_applied := public._evolved_apply_winner(v_rules, v_thresholds, v_state,
                                              p_game_uuid::text, v_win_rk[v_i],
                                              v_win_op[v_i], v_skill);
    v_state := v_applied.o_state;
    v_outcome := v_applied.o_outcome;
    if v_outcome is not null then
      v_out_keys := v_out_keys || v_win_rk[v_i];
      v_out_vals := v_out_vals || v_outcome;
    end if;
  end loop;

  select coalesce(jsonb_object_agg(k, v order by o), '{}'::jsonb)
    into v_outcomes
    from unnest(v_out_keys, v_out_vals) with ordinality as u(k, v, o);

  return public._evolved_state_json(v_state) || jsonb_build_object(
    'adjustments', to_jsonb(v_state.adjustments),
    'diagnostics', to_jsonb(v_diagnostics),
    'review_outcomes', v_outcomes,
    'revision', v_pos);
end;
$$;

revoke all on function public.evolved_replay(uuid) from anon, authenticated;
revoke execute on function public.evolved_replay(uuid) from public;

do $$
declare
  v_fn text;
begin
  foreach v_fn in array array[
    'public._evolved_thresholds(jsonb)',
    'public._evolved_fold_empty(bigint[])',
    'public._evolved_preset_skill(bigint[], text[], bigint)',
    'public._evolved_presets(uuid)',
    'public._evolved_apply_winner(jsonb, bigint[], public.evolved_fold_state, text, text, jsonb, text)',
    'public._evolved_state_json(public.evolved_fold_state)',
    'public._evolved_collection_diagnostics(uuid)',
    'public._evolved_resolve_claims(uuid)']
  loop
    execute format('revoke all on function %s from public, anon, authenticated', v_fn);
  end loop;
end;
$$;
