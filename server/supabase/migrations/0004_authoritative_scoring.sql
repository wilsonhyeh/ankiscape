-- 0004_authoritative_scoring.sql - Server-authoritative deterministic replay.
-- Append-only migration: never edit 0001-0003 as a rollout strategy.
--
-- Adds:
--   * public.evolved_rules()            - the checked-in rules snapshot.
--   * public.rules_versions population  - reproducible validation source.
--   * review_skip operation kind        - durable Classic-mode no-reward claims.
--   * deterministic replay helpers      - SHA-256 draws and rational rounding
--                                         matching the Python reducer exactly.
--   * public.evolved_replay(game_uuid)  - full canonical replay of stored ops.
--   * submit_operations v3              - validate envelope + policy, insert,
--                                         replay, and write state atomically.
--
-- Authority rules: client totals, item grants, and client-computed XP are
-- never accepted. Every reward is recomputed here from stored operations.

-- ---------------------------------------------------------------------------
-- 1. Rules snapshot (embedded; generated from shared/rules-v1.json).
-- ---------------------------------------------------------------------------
create or replace function public.evolved_rules()
returns jsonb
language sql
immutable
set search_path = public
as $fn$ select $rules${"achievements":{"note":"Counters are lifetime successful actions + derived levels, not inventory-on-hand. Replay/Undo determines state.","required":["first_catch","first_cook","skill_10_mining","skill_30_mining","skill_60_mining","skill_99_mining","skill_10_woodcutting","skill_30_woodcutting","skill_60_woodcutting","skill_99_woodcutting","skill_10_smithing","skill_30_smithing","skill_60_smithing","skill_99_smithing","skill_10_crafting","skill_30_crafting","skill_60_crafting","skill_99_crafting","skill_10_fishing","skill_30_fishing","skill_60_fishing","skill_99_fishing","skill_10_cooking","skill_30_cooking","skill_60_cooking","skill_99_cooking","cooks_100","cooks_1000"]},"bars":[{"base_xp":6.2,"display":"Bronze bar","id":"bronze_bar","level":1,"ore_required":{"Copper ore":1,"Tin ore":1},"tier":1},{"base_xp":12.5,"display":"Iron bar","id":"iron_bar","level":15,"ore_required":{"Iron ore":1},"tier":2},{"base_xp":13.67,"display":"Silver bar","id":"silver_bar","level":20,"ore_required":{"Silver ore":1},"tier":3},{"base_xp":17.5,"display":"Steel bar","id":"steel_bar","level":30,"ore_required":{"Coal":2,"Iron ore":1},"tier":4},{"base_xp":22.5,"display":"Gold bar","id":"gold_bar","level":40,"ore_required":{"Gold ore":1},"tier":5},{"base_xp":30.0,"display":"Mithril bar","id":"mithril_bar","level":50,"ore_required":{"Coal":4,"Mithril ore":1},"tier":6},{"base_xp":37.5,"display":"Adamantite bar","id":"adamantite_bar","level":70,"ore_required":{"Adamantite ore":1,"Coal":6},"tier":7},{"base_xp":50.0,"display":"Runite bar","id":"runite_bar","level":85,"ore_required":{"Coal":8,"Runite ore":1},"tier":8}],"burn":{"base":300,"den":1000,"floor":0,"per_level":2},"burn_xp_factor":{"den":4,"num":1},"crafting":[{"base_xp":1,"display":"Soft clay","id":"soft_clay","level":1,"requirements":{"Clay":1},"tier":1},{"base_xp":6.3,"display":"Unfired pot","id":"unfired_pot","level":1,"requirements":{"Soft clay":1},"tier":2},{"base_xp":6.3,"display":"Pot","id":"pot","level":1,"requirements":{"Unfired pot":1},"tier":3},{"base_xp":10,"display":"Pie dish","id":"pie_dish","level":1,"requirements":{"Unfired pie dish":1},"tier":4},{"base_xp":15,"display":"Bowl","id":"bowl","level":1,"requirements":{"Unfired bowl":1},"tier":5},{"base_xp":15,"display":"Gold ring","id":"gold_ring","level":5,"requirements":{"Gold bar":1},"tier":6},{"base_xp":20,"display":"Gold necklace","id":"gold_necklace","level":6,"requirements":{"Gold bar":1},"tier":7},{"base_xp":15,"display":"Unfired pie dish","id":"unfired_pie_dish","level":7,"requirements":{"Soft clay":1},"tier":8},{"base_xp":18,"display":"Unfired bowl","id":"unfired_bowl","level":8,"requirements":{"Soft clay":1},"tier":9},{"base_xp":50,"display":"Unstrung symbol","id":"unstrung_symbol","level":16,"requirements":{"Silver bar":1},"tier":10},{"base_xp":40,"display":"Sapphire ring","id":"sapphire_ring","level":20,"requirements":{"Gold bar":1,"Sapphire":1},"tier":11},{"base_xp":50,"display":"Sapphire","id":"sapphire","level":20,"requirements":{"Uncut sapphire":1},"tier":12},{"base_xp":60,"display":"Sapphire necklace","id":"sapphire_necklace","level":22,"requirements":{"Gold bar":1,"Sapphire":1},"tier":13},{"base_xp":52.5,"display":"Tiara","id":"tiara","level":23,"requirements":{"Silver bar":1},"tier":14},{"base_xp":67.5,"display":"Emerald","id":"emerald","level":27,"requirements":{"Uncut emerald":1},"tier":15},{"base_xp":55,"display":"Emerald ring","id":"emerald_ring","level":27,"requirements":{"Emerald":1,"Gold bar":1},"tier":16},{"base_xp":60,"display":"Emerald necklace","id":"emerald_necklace","level":29,"requirements":{"Emerald":1,"Gold bar":1},"tier":17},{"base_xp":70,"display":"Ruby ring","id":"ruby_ring","level":34,"requirements":{"Gold bar":1,"Ruby":1},"tier":18},{"base_xp":85,"display":"Ruby","id":"ruby","level":34,"requirements":{"Uncut ruby":1},"tier":19},{"base_xp":75,"display":"Ruby necklace","id":"ruby_necklace","level":40,"requirements":{"Gold bar":1,"Ruby":1},"tier":20},{"base_xp":85,"display":"Diamond ring","id":"diamond_ring","level":43,"requirements":{"Diamond":1,"Gold bar":1},"tier":21},{"base_xp":107.5,"display":"Diamond","id":"diamond","level":43,"requirements":{"Uncut diamond":1},"tier":22},{"base_xp":90,"display":"Diamond necklace","id":"diamond_necklace","level":56,"requirements":{"Diamond":1,"Gold bar":1},"tier":23}],"failed_gather_xp_factor":{"den":4,"num":1},"fish":[{"cooking_base_xp":13,"cooking_level":1,"display":"Shrimp","fishing_base_xp":10,"fishing_level":1,"id":"shrimp","probability":0.9,"tier":1},{"cooking_base_xp":26,"cooking_level":5,"display":"Sardine","fishing_base_xp":20,"fishing_level":5,"id":"sardine","probability":0.85,"tier":2},{"cooking_base_xp":65,"cooking_level":20,"display":"Trout","fishing_base_xp":50,"fishing_level":20,"id":"trout","probability":0.8,"tier":3},{"cooking_base_xp":104,"cooking_level":35,"display":"Tuna","fishing_base_xp":80,"fishing_level":35,"id":"tuna","probability":0.75,"tier":4},{"cooking_base_xp":117,"cooking_level":40,"display":"Lobster","fishing_base_xp":90,"fishing_level":40,"id":"lobster","probability":0.7,"tier":5},{"cooking_base_xp":130,"cooking_level":50,"display":"Swordfish","fishing_base_xp":100,"fishing_level":50,"id":"swordfish","probability":0.65,"tier":6},{"cooking_base_xp":156,"cooking_level":62,"display":"Monkfish","fishing_base_xp":120,"fishing_level":62,"id":"monkfish","probability":0.6,"tier":7},{"cooking_base_xp":182,"cooking_level":76,"display":"Shark","fishing_base_xp":140,"fishing_level":76,"id":"shark","probability":0.55,"tier":8},{"cooking_base_xp":208,"cooking_level":82,"display":"Anglerfish","fishing_base_xp":160,"fishing_level":82,"id":"anglerfish","probability":0.5,"tier":9}],"gathering":{"base_probability":0.8,"cap":0.95,"level_bonus_factor":0.02},"gem_drop":{"den":256,"num":1},"gems":[{"base_xp":50,"display":"Uncut sapphire","id":"uncut_sapphire","order":1,"probability_den":4,"probability_num":1},{"base_xp":67.5,"display":"Uncut emerald","id":"uncut_emerald","order":2,"probability_den":8,"probability_num":1},{"base_xp":85,"display":"Uncut ruby","id":"uncut_ruby","order":3,"probability_den":16,"probability_num":1},{"base_xp":107.5,"display":"Uncut diamond","id":"uncut_diamond","order":4,"probability_den":64,"probability_num":1}],"micro_xp_per_xp":1000000,"ores":[{"base_xp":5.0,"display":"Rune essence","id":"rune_essence","level":1,"probability":0.95,"tier":1},{"base_xp":5.0,"display":"Clay","id":"clay","level":1,"probability":0.9,"tier":2},{"base_xp":17.5,"display":"Copper ore","id":"copper_ore","level":1,"probability":0.85,"tier":3},{"base_xp":17.5,"display":"Tin ore","id":"tin_ore","level":1,"probability":0.85,"tier":4},{"base_xp":35,"display":"Iron ore","id":"iron_ore","level":15,"probability":0.8,"tier":5},{"base_xp":40,"display":"Silver ore","id":"silver_ore","level":20,"probability":0.75,"tier":6},{"base_xp":50,"display":"Coal","id":"coal","level":30,"probability":0.7,"tier":7},{"base_xp":65.0,"display":"Gold ore","id":"gold_ore","level":40,"probability":0.65,"tier":8},{"base_xp":80,"display":"Mithril ore","id":"mithril_ore","level":55,"probability":0.6,"tier":9},{"base_xp":95.0,"display":"Adamantite ore","id":"adamantite_ore","level":70,"probability":0.55,"tier":10},{"base_xp":125,"display":"Runite ore","id":"runite_ore","level":85,"probability":0.5,"tier":11}],"practice_xp":1,"protocol_version":1,"rules_version":1,"thresholds":[0,83,174,276,388,512,650,801,969,1154,1358,1584,1833,2107,2411,2746,3115,3523,3973,4470,5018,5624,6291,7028,7842,8740,9730,10824,12031,13363,14833,16456,18247,20224,22406,24815,27473,30408,33648,37224,41171,45529,50339,55649,61512,67983,75127,83014,91721,101333,111945,123660,136594,150872,166636,184040,203254,224466,247886,273742,302288,333804,368599,407015,449428,496254,547953,605032,668051,737627,814445,899257,992895,1096278,1210421,1336443,1475581,1629200,1798808,1986068,2192818,2421087,2673114,2951373,3258594,3597792,3972294,4385776,4842295,5346332,5902831,6517253,7195629,7944614,8771558,9684577,10692629,11805606,13034431],"tier_multiplier":{"base":100,"cap_den":1,"cap_num":2,"den":100,"per_tier":5},"trees":[{"base_xp":25,"display":"Tree","id":"tree","level":1,"probability":0.9,"tier":1},{"base_xp":37.5,"display":"Oak","id":"oak","level":15,"probability":0.85,"tier":2},{"base_xp":67.5,"display":"Willow","id":"willow","level":30,"probability":0.8,"tier":3},{"base_xp":85,"display":"Teak","id":"teak","level":35,"probability":0.75,"tier":4},{"base_xp":100,"display":"Maple","id":"maple","level":45,"probability":0.7,"tier":5},{"base_xp":125,"display":"Mahogany","id":"mahogany","level":50,"probability":0.65,"tier":6},{"base_xp":175,"display":"Yew","id":"yew","level":60,"probability":0.6,"tier":7},{"base_xp":250,"display":"Magic","id":"magic","level":75,"probability":0.55,"tier":8},{"base_xp":380,"display":"Redwood","id":"redwood","level":90,"probability":0.5,"tier":9}]}$rules$::jsonb $fn$;

comment on function public.evolved_rules() is
  'Frozen rules-v1 snapshot; must match shared/rules-v1.json (maintainer verify).';

insert into public.rules_versions(version, rules)
values (1, public.evolved_rules())
on conflict (version) do update set rules = excluded.rules;

-- ---------------------------------------------------------------------------
-- 2. Operation kinds: review_skip joins the allowlist.
-- ---------------------------------------------------------------------------
alter table public.game_operations
  drop constraint if exists game_operations_kind_check;
alter table public.game_operations
  add constraint game_operations_kind_check check (
    kind in ('review_award','review_retract','review_restore',
             'catchup_preset','review_skip'));

-- ---------------------------------------------------------------------------
-- 3. Deterministic replay helpers (integer-exact; no floats).
-- ---------------------------------------------------------------------------

create or replace function public._evolved_mul_half_up(micro bigint, num bigint, den bigint)
returns bigint
language sql
immutable
as $$
  select (2 * micro * num + den) / (2 * den)
$$;

-- Decimal text -> exact [numerator, denominator] (e.g. "0.95" -> [95,100]).
create or replace function public._evolved_dec_frac(v text)
returns bigint[]
language plpgsql
immutable
as $$
declare
  s text := trim(both from v);
  dot int;
  scale int;
begin
  if s is null or s = '' then
    raise exception 'bad decimal %', v using errcode = '22000';
  end if;
  dot := position('.' in s);
  if dot = 0 then
    return array[s::bigint, 1::bigint];
  end if;
  scale := length(s) - dot;
  return array[replace(s, '.', '')::bigint, (10::numeric ^ scale)::bigint];
end;
$$;

-- base_xp (jsonb number) -> micro-XP, half-up (matches Python Decimal).
create or replace function public._evolved_base_micro(p_base jsonb)
returns bigint
language plpgsql
immutable
as $$
declare
  f bigint[];
begin
  f := public._evolved_dec_frac(p_base::text);
  return public._evolved_mul_half_up(f[1] * 1000000, 1::bigint, f[2]);
end;
$$;

-- Tier multiplier (100 + 5*(tier-1), capped at 200) applied half-up.
create or replace function public._evolved_mult_micro(base_micro bigint, tier int)
returns bigint
language sql
immutable
as $$
  select public._evolved_mul_half_up(
    base_micro,
    least(100 + 5 * (greatest(coalesce(tier, 1), 1) - 1), 200)::bigint,
    100::bigint)
$$;

-- Exact rational compare: r / 2^48 < num / den.
create or replace function public._evolved_frac_hit(r bigint, num bigint, den bigint)
returns boolean
language sql
immutable
as $$
  select case
    when den <= 0 or num < 0 then false
    when num = 0 then false
    when num >= den then true
    else r * den < num * (1::bigint << 48)
  end
$$;

-- SHA-256 deterministic draw. Input: compact JSON array with no spaces.
create or replace function public._evolved_draw(rules_version int, game text, rk text, label text)
returns bigint
language sql
immutable
as $$
  select ('x' || substr(encode(extensions.digest(
      ('[' || rules_version || ',"' || game || '","' || rk || '","' || label || '"]')::bytea,
      'sha256'), 'hex'), 1, 12))::bit(48)::bigint
$$;

-- One gathering action draw: min(0.80 + 0.02*level, 0.95) * resource probability.
create or replace function public._evolved_action_hit(
  rules_version int, player_level int, p_prob jsonb, game text, rk text)
returns boolean
language plpgsql
immutable
as $$
declare
  f bigint[] := public._evolved_dec_frac(p_prob::text);
  num bigint;
  den bigint;
  r bigint;
begin
  num := least(80 + 2 * greatest(coalesce(player_level, 1), 1), 95)::bigint * f[1];
  den := 100::bigint * f[2];
  r := public._evolved_draw(rules_version, game, rk, 'action');
  return public._evolved_frac_hit(r, num, den);
end;
$$;

-- Skill index used by the reducer's level/inventory arrays.
create or replace function public._evolved_skill_idx(skill text)
returns int
language sql
immutable
as $$
  select case lower(coalesce(skill, ''))
    when 'mining' then 1
    when 'woodcutting' then 2
    when 'smithing' then 3
    when 'crafting' then 4
    when 'fishing' then 5
    when 'cooking' then 6
    else 0
  end
$$;

create or replace function public._evolved_skill_name(idx int)
returns text
language sql
immutable
as $$
  select case idx
    when 1 then 'mining'
    when 2 then 'woodcutting'
    when 3 then 'smithing'
    when 4 then 'crafting'
    when 5 then 'fishing'
    when 6 then 'cooking'
    else null
  end
$$;

-- Gem pick: cumulative legacy distribution (1/4, 1/8, 1/16, 1/64) over r/2^48.
create or replace function public._evolved_gem_pick(p_rules jsonb, r bigint)
returns jsonb
language plpgsql
immutable
as $$
declare
  g jsonb;
  cum_num bigint := 0;
  cum_den bigint := 1;
  n bigint;
  d bigint;
begin
  for g in
    select value from jsonb_array_elements(p_rules->'gems')
    order by (value->>'order')::int
  loop
    n := (g->>'probability_num')::bigint;
    d := (g->>'probability_den')::bigint;
    cum_num := cum_num * d + n * cum_den;
    cum_den := cum_den * d;
    if public._evolved_frac_hit(r, cum_num, cum_den) then
      return g;
    end if;
    if cum_num % 2 = 0 and cum_den % 2 = 0 then
      cum_num := cum_num / 2;
      cum_den := cum_den / 2;
    end if;
  end loop;
  return null;
end;
$$;

create or replace function public._evolved_find(p_rules jsonb, p_table text, p_display text)
returns jsonb
language sql
immutable
as $$
  select value from jsonb_array_elements(coalesce(p_rules->p_table, '[]'::jsonb))
  where value->>'display' = p_display
  limit 1
$$;

create or replace function public._evolved_first_unlocked(
  p_rules jsonb, p_table text, p_level_key text, p_level int)
returns jsonb
language sql
immutable
as $$
  select value from jsonb_array_elements(coalesce(p_rules->p_table, '[]'::jsonb))
  where p_level >= coalesce((value->>p_level_key)::int, 1)
  order by (value->>'tier')::int
  limit 1
$$;

create or replace function public._evolved_highest_unlocked(
  p_rules jsonb, p_table text, p_level_key text, p_level int)
returns jsonb
language sql
immutable
as $$
  select value from jsonb_array_elements(coalesce(p_rules->p_table, '[]'::jsonb))
  where p_level >= coalesce((value->>p_level_key)::int, 1)
  order by (value->>'tier')::int desc
  limit 1
$$;

-- ---------------------------------------------------------------------------
-- 4. Pure direct-action application (mirrors reducer._apply_direct).
-- ---------------------------------------------------------------------------
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

-- Catch-up routing: preset skill -> highest unlocked resource at that level.
create or replace function public._evolved_apply_catchup(
  p_rules jsonb, p_skill text, p_levels bigint[], p_inv jsonb,
  p_game text, p_rk text, p_policy int)
returns jsonb
language plpgsql
immutable
as $$
declare
  v_idx int := public._evolved_skill_idx(p_skill);
  v_level bigint;
  v_spec jsonb;
begin
  if v_idx = 0 then
    return public._evolved_apply_direct(p_rules, 'mining', '', p_levels,
                                        p_inv, p_game, p_rk, p_policy);
  end if;
  v_level := p_levels[v_idx];
  if p_skill = 'mining' then
    v_spec := public._evolved_highest_unlocked(p_rules, 'ores', 'level', v_level::int);
    if v_spec is null then
      v_spec := public._evolved_first_unlocked(p_rules, 'ores', 'level', v_level::int);
    end if;
  elsif p_skill = 'woodcutting' then
    v_spec := public._evolved_highest_unlocked(p_rules, 'trees', 'level', v_level::int);
    if v_spec is null then
      v_spec := public._evolved_first_unlocked(p_rules, 'trees', 'level', v_level::int);
    end if;
  else
    v_spec := public._evolved_highest_unlocked(p_rules, 'fish', 'fishing_level', v_level::int);
    if v_spec is null then
      v_spec := public._evolved_first_unlocked(p_rules, 'fish', 'fishing_level', v_level::int);
    end if;
  end if;
  return public._evolved_apply_direct(p_rules, p_skill, v_spec->>'display',
                                      p_levels, p_inv, p_game, p_rk, p_policy);
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. Full canonical replay for one game.
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- 6. submit_operations v3: strict validation + atomic replay + state write.
-- ---------------------------------------------------------------------------
create or replace function public.submit_operations(p_game_uuid uuid, p_ops jsonb)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_owner uuid;
  v_op jsonb;
  v_accepted uuid[] := '{}';
  v_conflicts jsonb[] := '{}';
  v_n int;
  v_new int := 0;
  v_existing public.game_operations%rowtype;
  v_kind text;
  v_policy int;
  v_device text;
  v_seq bigint;
  v_lamport bigint;
  v_state jsonb;
  v_rev bigint;
  v_headers text;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  perform public._assert_verified(v_user);
  -- Protocol transition: HTTP submissions must announce protocol 2. Direct
  -- SQL (migrations, pgTAP, parity vectors) has no request headers and is
  -- allowed; legacy pending operations imported through maintainer tooling
  -- also arrive without headers by design.
  v_headers := nullif(current_setting('request.headers', true), '');
  if v_headers is not null
     and coalesce((v_headers::json)->>'x-ankiscape-protocol', '') <> '2' then
    raise exception 'client_update_required' using errcode = '22000';
  end if;
  if jsonb_typeof(p_ops) != 'array' then
    raise exception 'invalid_ops' using errcode = '22000';
  end if;
  v_n := jsonb_array_length(p_ops);
  if v_n < 1 or v_n > 200 then
    raise exception 'ops_batch_size' using errcode = '22000';
  end if;
  if octet_length(p_ops::text) > 262144 then
    raise exception 'ops_batch_bytes' using errcode = '22000';
  end if;
  select user_id into v_owner from public.players where game_uuid = p_game_uuid for update;
  if not found or v_owner != v_user then
    raise exception 'game_mismatch' using errcode = '42501';
  end if;

  -- 1. Validate every envelope before writing anything.
  for v_op in select * from jsonb_array_elements(p_ops) loop
    v_kind := v_op->>'kind';
    if v_kind is null or v_kind not in
       ('review_award','review_retract','review_restore',
        'catchup_preset','review_skip') then
      raise exception 'invalid_kind:%', coalesce(v_kind, '') using errcode = '22000';
    end if;
    if coalesce(v_op->>'op_id', '') = '' then
      raise exception 'invalid_op_id' using errcode = '22000';
    end if;
    begin
      perform (v_op->>'op_id')::uuid;
    exception when others then
      raise exception 'invalid_op_id' using errcode = '22000';
    end;
    v_device := v_op->>'device_id';
    if v_device is null or v_device !~ '^[A-Za-z0-9_-]{1,64}$' then
      raise exception 'invalid_device' using errcode = '22000';
    end if;
    begin
      v_seq := (v_op->>'device_seq')::bigint;
      v_lamport := (v_op->>'lamport')::bigint;
    exception when others then
      raise exception 'invalid_envelope' using errcode = '22000';
    end;
    if v_seq is null or v_seq <= 0 or v_lamport is null or v_lamport < 0 then
      raise exception 'invalid_envelope' using errcode = '22000';
    end if;
    if jsonb_typeof(coalesce(v_op->'payload', '{}'::jsonb)) != 'object' then
      raise exception 'invalid_payload' using errcode = '22000';
    end if;
    if v_kind = 'review_award' then
      v_policy := coalesce((v_op->'payload'->>'reward_policy')::int, 1);
      if v_policy not in (1, 2) then
        raise exception 'unsupported_reward_policy:%', v_policy using errcode = '22000';
      end if;
      if coalesce(v_op->'payload'->>'review_key', '') = '' then
        raise exception 'invalid_review_key' using errcode = '22000';
      end if;
    elsif v_kind in ('review_retract','review_restore') then
      if coalesce(v_op->'payload'->>'target_review_key', '') = '' then
        raise exception 'invalid_review_key' using errcode = '22000';
      end if;
    elsif v_kind = 'review_skip' then
      if coalesce(v_op->'payload'->>'review_key', '') = '' then
        raise exception 'invalid_review_key' using errcode = '22000';
      end if;
    elsif v_kind = 'catchup_preset' then
      if lower(coalesce(v_op->'payload'->>'skill', '')) not in
         ('mining','woodcutting','fishing') then
        raise exception 'invalid_preset_skill' using errcode = '22000';
      end if;
    end if;
  end loop;

  -- 2. Insert new operations; exact retries are idempotent, changed envelopes
  --    are conflicts. Neither awards twice nor advances the revision.
  for v_op in select * from jsonb_array_elements(p_ops) loop
    begin
      insert into public.game_operations(
        op_id, game_uuid, user_id, device_id, device_seq, lamport, kind, payload, payload_hash)
      values (
        (v_op->>'op_id')::uuid, p_game_uuid, v_user,
        v_op->>'device_id', (v_op->>'device_seq')::bigint, (v_op->>'lamport')::bigint,
        v_op->>'kind', coalesce(v_op->'payload','{}'::jsonb),
        encode(extensions.digest(
          coalesce(v_op->'payload','{}'::jsonb)::text::bytea, 'sha256'), 'hex'));
      v_accepted := v_accepted || (v_op->>'op_id')::uuid;
      v_new := v_new + 1;
    exception when unique_violation then
      select * into v_existing from public.game_operations
        where op_id = (v_op->>'op_id')::uuid;
      if found
         and v_existing.device_id = v_op->>'device_id'
         and v_existing.device_seq = (v_op->>'device_seq')::bigint
         and v_existing.lamport = (v_op->>'lamport')::bigint
         and v_existing.kind = v_op->>'kind'
         and v_existing.payload = coalesce(v_op->'payload','{}'::jsonb) then
        v_accepted := v_accepted || (v_op->>'op_id')::uuid;
      else
        v_conflicts := v_conflicts || jsonb_build_object(
          'op_id', v_op->>'op_id', 'error', 'id_conflict');
      end if;
    end;
  end loop;

  -- 3. Recompute state from stored operations when anything new landed.
  if v_new > 0 then
    v_state := public.evolved_replay(p_game_uuid);
    select coalesce(revision, 0) into v_rev from public.game_state
      where game_uuid = p_game_uuid for update;
    update public.game_state set
      revision = coalesce(v_rev, 0) + 1,
      xp = coalesce(v_state->'xp_micro', '{}'::jsonb),
      inventory = coalesce(v_state->'inventory', '{}'::jsonb),
      counters = coalesce(v_state->'counters', '{}'::jsonb),
      achievements = coalesce(v_state->'achievements', '[]'::jsonb),
      checkpoint = jsonb_build_object(
        'levels', coalesce(v_state->'levels', '{}'::jsonb),
        'diagnostics', coalesce(v_state->'diagnostics', '[]'::jsonb)),
      updated_at = now()
      where game_uuid = p_game_uuid;
    if found then
      insert into public.game_checkpoints(game_uuid, revision, state)
      values (p_game_uuid, coalesce(v_rev, 0) + 1, v_state)
      on conflict (game_uuid, revision) do nothing;
    end if;
  end if;

  return jsonb_build_object('accepted', to_jsonb(v_accepted),
                            'conflicts', to_jsonb(v_conflicts),
                            'applied', v_new);
end;
$$;
revoke all on function public.submit_operations(uuid, jsonb) from anon, authenticated;
grant execute on function public.submit_operations(uuid, jsonb) to authenticated;

-- Server capability marker: old servers lack it; old clients can detect the
-- authoritative-scoring release before submitting new-protocol operations.
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
    'operation_kinds', jsonb_build_array(
      'review_award','review_retract','review_restore','catchup_preset','review_skip'))
$$;
revoke all on function public.evolved_capabilities() from anon, authenticated;
grant execute on function public.evolved_capabilities() to anon, authenticated;

revoke execute on function public.link_game(uuid) from public;
revoke execute on function public.submit_operations(uuid, jsonb) from public;
revoke execute on function public.fetch_operations(uuid, bigint, int) from public;
revoke execute on function public.get_game_state(uuid) from public;
revoke execute on function public._assert_verified(uuid) from public;
revoke execute on function public.evolved_capabilities() from public;
grant execute on function public.evolved_capabilities() to anon, authenticated;
revoke execute on function public.hiscores(text, int) from public;
grant execute on function public.hiscores(text, int) to anon, authenticated;
revoke execute on function public.public_profile(text) from public;
grant execute on function public.public_profile(text) to anon, authenticated;
