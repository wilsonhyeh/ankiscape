-- 0018_operation_archive.sql - archive old operations into compressed
-- segments (plan: HQ plans/ankiscape-incremental-scoring-and-compaction.md,
-- Phase 2). Append-only migration: never edit 0001-0017 as a rollout strategy.
-- Re-runnable: every object is created idempotently.
--
-- Why: a live game_operations row costs ~1,121 bytes on disk (heap + five
-- indexes). Production fills the Supabase Free 500 MB cap in months at
-- today's upload rate, and one heavy player adds ~245 MB a year.
--
-- What:
--   * game_operation_segments - 1,000 consecutive operations of one game per
--                               row: the exact to_jsonb(game_operations row)
--                               objects in id order, lz4-compressed.
--   * game_compaction_marks   - per (game, device): the highest archived
--                               device_seq, so retries of archived operations
--                               are still idempotent.
--   * game_operations_all     - live union archived rows, same columns.
--   * _evolved_compact_game   - archives the longest id-ordered prefix of
--                               operations that are older than 7 days, at
--                               least 2,000 canonical positions behind the
--                               newest, and before the oldest fold snapshot.
--                               Only whole 1,000-op segments. Daily pg_cron.
--   * fetch_operations        - pages read archive then live; byte-identical
--                               to the pages before compaction.
--   * evolved_replay, the fold rebuild and its helpers read live + archive.
--   * submit_operations       - retries of archived (device, seq) slots are
--                               accepted or id_conflict, as for live rows.
--   * ops_health()            - adds the archive numbers.
-- Nothing is deleted from history: every archived operation stays
-- downloadable and scored. The add-on does not change.
--
-- The fold fast path (0017 _evolved_fold_fast) is not re-issued: it reads
-- only the upload's new operations and, on undo, the operations after a
-- fold snapshot. Compaction never archives an operation at or after the
-- oldest snapshot, and every later snapshot is newer still, so those reads
-- only ever need live rows. An undo whose winner is archived finds no
-- snapshot before it and falls back to a full rebuild.

-- ---------------------------------------------------------------------------
-- 1. Tables and view
-- ---------------------------------------------------------------------------
create table if not exists public.game_operation_segments (
  game_uuid uuid not null,
  user_id uuid not null references public.players(user_id) on delete cascade,
  first_id bigint not null,
  last_id bigint not null,
  op_count int not null check (op_count > 0),
  -- catch-up presets inside this segment; _evolved_presets reads only
  -- segments with presets, so an upload never decompresses the archive.
  preset_count int not null default 0,
  ops jsonb not null,
  created_at timestamptz not null default now(),
  primary key (game_uuid, first_id),
  check (last_id >= first_id)
);
-- Fails loudly on a server built without lz4 (no silent fallback to pglz).
alter table public.game_operation_segments alter column ops set compression lz4;

create table if not exists public.game_compaction_marks (
  game_uuid uuid not null,
  device_id text not null,
  user_id uuid not null references public.players(user_id) on delete cascade,
  max_seq bigint not null,
  primary key (game_uuid, device_id)
);

alter table public.game_operation_segments enable row level security;
alter table public.game_compaction_marks enable row level security;
revoke all on public.game_operation_segments from anon, authenticated;
revoke all on public.game_compaction_marks from anon, authenticated;

-- Archived rows come back through jsonb_populate_record, so every column has
-- exactly the type it had in game_operations. game_uuid comes from the
-- segment so a game filter reaches the segments' primary key.
create or replace view public.game_operations_all
with (security_invoker = true) as
  select o.id, o.op_id, o.game_uuid, o.user_id, o.device_id, o.device_seq,
         o.lamport, o.kind, o.payload, o.payload_hash, o.created_at
    from public.game_operations o
  union all
  select r.id, r.op_id, s.game_uuid, r.user_id, r.device_id, r.device_seq,
         r.lamport, r.kind, r.payload, r.payload_hash, r.created_at
    from public.game_operation_segments s
    cross join lateral jsonb_array_elements(s.ops) as e(v)
    cross join lateral jsonb_populate_record(null::public.game_operations, e.v) as r;
revoke all on public.game_operations_all from anon, authenticated;

-- Live + archived operation count for one game, without decompressing.
create or replace function public._evolved_total_ops(p_game uuid)
returns bigint
language sql
stable
set search_path = public
as $$
  select (select count(*) from public.game_operations where game_uuid = p_game)
       + (select coalesce(sum(op_count), 0) from public.game_operation_segments
           where game_uuid = p_game)
$$;

-- The archived operation in one (game, device, seq) slot, or null.
create or replace function public._evolved_archived_op(p_game uuid, p_device text, p_seq bigint)
returns jsonb
language sql
stable
set search_path = public
as $$
  select e.v
    from public.game_operation_segments s
    cross join lateral jsonb_array_elements(s.ops) as e(v)
   where s.game_uuid = p_game
     and s.ops @> jsonb_build_array(jsonb_build_object('device_id', p_device,
                                                       'device_seq', p_seq))
     and e.v->>'device_id' = p_device
     and (e.v->>'device_seq')::bigint = p_seq
   limit 1
$$;

-- ---------------------------------------------------------------------------
-- 2. Scoring reads live + archive. Bodies are 0016/0017 with only the
--    operation source changed.
-- ---------------------------------------------------------------------------
-- Presets: live rows through the 0017 partial index, plus archived presets
-- from the segments that hold any (preset_count > 0).
create or replace function public._evolved_presets(
  p_game uuid, out o_ts bigint[], out o_skill text[])
language sql
stable
set search_path = public
as $$
  with p as (
    select payload, lamport, device_id, device_seq, op_id
      from public.game_operations
     where game_uuid = p_game and kind = 'catchup_preset'
    union all
    select r.payload, r.lamport, r.device_id, r.device_seq, r.op_id
      from public.game_operation_segments s
      cross join lateral jsonb_array_elements(s.ops) as e(v)
      cross join lateral jsonb_populate_record(null::public.game_operations, e.v) as r
     where s.game_uuid = p_game and s.preset_count > 0 and r.kind = 'catchup_preset'
  )
  select coalesce(array_agg(coalesce((payload->>'effective_ts')::bigint, 0)
                            order by coalesce((payload->>'effective_ts')::bigint, 0),
                                     lamport, device_id, device_seq, op_id), '{}'),
         coalesce(array_agg(lower(payload->>'skill')
                            order by coalesce((payload->>'effective_ts')::bigint, 0),
                                     lamport, device_id, device_seq, op_id), '{}')
    from p
   where lower(coalesce(payload->>'skill', '')) in ('mining','woodcutting','fishing')
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
        from public.game_operations_all
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
      from public.game_operations_all
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
    from public.game_operations_all o
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
  v_pos := public._evolved_total_ops(p_game_uuid);
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

-- ---------------------------------------------------------------------------
-- 3. Full fold rebuild (0017 body; reads live + archive).
-- ---------------------------------------------------------------------------
create or replace function public._evolved_fold_rebuild(p_game uuid, p_bump boolean default true)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_rules jsonb := public.evolved_rules();
  v_thresholds bigint[] := public._evolved_thresholds(v_rules);
  v_rv int := coalesce((v_rules->>'rules_version')::int, 1);
  v_state public.evolved_fold_state := public._evolved_fold_empty(v_thresholds);
  v_applied record;
  v_user uuid;
  v_bad record;
  v_ts bigint[];
  v_sk text[];
  v_claim_diag text[];
  v_diag text[];
  v_winners jsonb;
  v_total bigint;
  v_n bigint := 0;
  v_op record;
  v_skill text;
  v_wl bigint;
  v_wd text;
  v_ws bigint;
  v_wo uuid;
begin
  select user_id into v_user from public.game_state where game_uuid = p_game;
  if v_user is null then
    select user_id into v_user from public.players where game_uuid = p_game;
  end if;
  if v_user is null then
    return;
  end if;

  select o.op_id::text as op_id, coalesce((o.payload->>'reward_policy')::int, 1) as policy
    into v_bad
    from public.game_operations_all o
   where o.game_uuid = p_game and o.kind = 'review_award'
     and coalesce(o.payload->>'review_key', '') <> ''
     and coalesce((o.payload->>'reward_policy')::int, 1) not in (1, 2)
   order by o.lamport, o.device_id, o.device_seq, o.op_id
   limit 1;
  if found then
    raise exception 'unsupported_policy:% op %', v_bad.policy, v_bad.op_id
      using errcode = '22000';
  end if;

  select o_ts, o_skill into v_ts, v_sk from public._evolved_presets(p_game);

  delete from public.game_review_keys where game_uuid = p_game;
  with c as (select * from public._evolved_resolve_claims(p_game)),
  ins as (
    insert into public.game_review_keys(game_uuid, review_key, user_id, status, provenance,
                                        winner_op, w_lamport, w_device, w_seq, simple)
    select p_game, c.review_key, v_user, c.status, c.provenance, c.winner_op,
           c.w_lamport, c.w_device, c.w_seq, c.simple
      from c
    returning 1)
  select coalesce(array_agg(c.diag order by c.first_pos) filter (where c.diag is not null), '{}'),
         coalesce(jsonb_object_agg(c.winner_op::text, c.review_key)
                    filter (where c.status = 'active' and c.winner_op is not null), '{}'::jsonb)
    into v_claim_diag, v_winners
    from c;
  v_diag := array(select d from unnest(public._evolved_collection_diagnostics(p_game) || v_claim_diag) d
                  order by d);

  delete from public.game_fold_snapshots where game_uuid = p_game;
  v_total := public._evolved_total_ops(p_game);

  for v_op in
    select op_id, kind, payload, lamport, device_id, device_seq
      from public.game_operations_all
     where game_uuid = p_game
     order by lamport, device_id, device_seq, op_id
  loop
    v_n := v_n + 1;
    if v_op.kind = 'review_award' and v_winners ? v_op.op_id::text then
      v_skill := null;
      if coalesce(v_op.payload->>'provenance', 'catchup') <> 'direct' then
        v_skill := public._evolved_preset_skill(
          v_ts, v_sk, coalesce((v_op.payload->>'review_ts')::bigint, 0));
      end if;
      v_applied := public._evolved_apply_winner(v_rules, v_thresholds, v_state,
        p_game::text, v_winners->>(v_op.op_id::text), v_op.payload, v_skill);
      v_state := v_applied.o_state;
    end if;
    if v_n % 100 = 0 and v_n > v_total - 2000 then
      perform public._evolved_fold_snapshot(p_game, v_user, v_n, v_op.lamport,
        v_op.device_id, v_op.device_seq, v_op.op_id, v_state);
    end if;
    v_wl := v_op.lamport; v_wd := v_op.device_id; v_ws := v_op.device_seq; v_wo := v_op.op_id;
  end loop;

  insert into public.game_fold(game_uuid, user_id, rules_version, wm_lamport, wm_device,
                               wm_seq, wm_op, folded_ops, state, diagnostics, stale,
                               stale_since, rebuilt_at, updated_at)
  values (p_game, v_user, v_rv, v_wl, v_wd, v_ws, v_wo, v_n, v_state, v_diag, false,
          null, now(), now())
  on conflict (game_uuid) do update set
    user_id = excluded.user_id, rules_version = excluded.rules_version,
    wm_lamport = excluded.wm_lamport, wm_device = excluded.wm_device,
    wm_seq = excluded.wm_seq, wm_op = excluded.wm_op,
    folded_ops = excluded.folded_ops, state = excluded.state,
    diagnostics = excluded.diagnostics, stale = false, stale_since = null,
    rebuilt_at = now(), updated_at = now();

  perform public._evolved_state_write(p_game, v_state, v_diag, p_bump);
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. submit_operations: the 0017 body plus the archive idempotency check and
--    an archive-aware operation count.
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
  v_new_ids uuid[] := '{}';
  v_total bigint;
  v_limit int;
  v_arch jsonb;
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
    -- 0018: a (device, seq) slot that was archived is checked against the
    -- archive (the live unique constraints no longer see it), with the same
    -- rule as a live row: identical envelope -> accepted, else id_conflict.
    v_arch := null;
    if exists (select 1 from public.game_compaction_marks m
                where m.game_uuid = p_game_uuid and m.device_id = v_op->>'device_id'
                  and m.max_seq >= (v_op->>'device_seq')::bigint) then
      v_arch := public._evolved_archived_op(p_game_uuid, v_op->>'device_id',
                                            (v_op->>'device_seq')::bigint);
    end if;
    if v_arch is not null then
      if (v_arch->>'op_id')::uuid = (v_op->>'op_id')::uuid
         and (v_arch->>'lamport')::bigint = (v_op->>'lamport')::bigint
         and v_arch->>'kind' = v_op->>'kind'
         and v_arch->'payload' = coalesce(v_op->'payload','{}'::jsonb) then
        v_accepted := v_accepted || (v_op->>'op_id')::uuid;
      else
        v_conflicts := v_conflicts || jsonb_build_object(
          'op_id', v_op->>'op_id', 'error', 'id_conflict');
      end if;
      continue;
    end if;
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
      v_new_ids := v_new_ids || (v_op->>'op_id')::uuid;
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

  -- 3. Score the new operations (0017): fold them onto the stored state; if
  --    that cannot be proven equal to a full replay, rebuild now for small
  --    games, or mark the fold stale for the background job.
  if v_new > 0 then
    begin
      perform public._evolved_fold_fast(p_game_uuid, v_new_ids);
    exception when sqlstate 'AS001' then
      select folded_ops into v_total from public.game_fold where game_uuid = p_game_uuid;
      if v_total is null then
        v_total := public._evolved_total_ops(p_game_uuid);
      else
        v_total := v_total + v_new;
      end if;
      v_limit := coalesce(nullif(current_setting('ankiscape.sync_rebuild_max_ops', true), '')::int,
                          3000);
      if v_total <= v_limit then
        perform public._evolved_fold_rebuild(p_game_uuid, true);
      else
        insert into public.game_fold(game_uuid, user_id, rules_version, state, stale, stale_since)
        values (p_game_uuid, v_owner,
                coalesce((public.evolved_rules()->>'rules_version')::int, 1),
                public._evolved_fold_empty(public._evolved_thresholds(public.evolved_rules())),
                true, now())
        on conflict (game_uuid) do update set
          stale = true,
          stale_since = coalesce(public.game_fold.stale_since, now()),
          updated_at = now();
      end if;
    end;
  end if;

  return jsonb_build_object('accepted', to_jsonb(v_accepted),
                            'conflicts', to_jsonb(v_conflicts),
                            'applied', v_new);
end;
$$;
revoke all on function public.submit_operations(uuid, jsonb) from anon, authenticated;
grant execute on function public.submit_operations(uuid, jsonb) to authenticated;

-- ---------------------------------------------------------------------------
-- 5. fetch_operations: the 0002 body (same signature, guards and keys), with
--    pages built from the archive first. Every archived id of a game is below
--    its live ids (a segment is an id-ordered prefix of the live rows when it
--    is written), so archive-then-live is id order. Archived rows are
--    re-rendered through the game_operations row type, so each element is
--    exactly what to_jsonb(live row) gave before compaction.
-- ---------------------------------------------------------------------------
create or replace function public.fetch_operations(p_game_uuid uuid, p_cursor bigint, p_limit int)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_owner uuid;
  v_lim int := least(greatest(coalesce(p_limit, 100), 1), 200);
  v_rows jsonb := '[]'::jsonb;
  v_next bigint := coalesce(p_cursor, 0);
  v_rev bigint;
  v_first bigint;
  v_part jsonb;
  v_k int;
  v_max bigint;
  v_n int := 0;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select user_id into v_owner from public.players where game_uuid = p_game_uuid;
  if not found or v_owner != v_user then
    raise exception 'game_mismatch' using errcode = '42501';
  end if;
  -- Archive: only the segments the page reaches are decompressed.
  for v_first in
    select s.first_id from public.game_operation_segments s
     where s.game_uuid = p_game_uuid and s.last_id > v_next
     order by s.first_id
  loop
    select coalesce(jsonb_agg(to_jsonb(r) order by r.id), '[]'::jsonb), count(*), max(r.id)
      into v_part, v_k, v_max
      from (select x.*
              from public.game_operation_segments s
              cross join lateral jsonb_array_elements(s.ops) as e(v)
              cross join lateral jsonb_populate_record(null::public.game_operations, e.v) as x
             where s.game_uuid = p_game_uuid and s.first_id = v_first and x.id > v_next
             order by x.id limit v_lim - v_n) r;
    v_rows := v_rows || v_part;
    v_n := v_n + v_k;
    v_next := coalesce(v_max, v_next);
    exit when v_n >= v_lim;
  end loop;
  if v_n < v_lim then
    select coalesce(jsonb_agg(to_jsonb(o) order by o.id), '[]'::jsonb), max(o.id)
      into v_part, v_max
      from (select * from public.game_operations
            where game_uuid = p_game_uuid and id > v_next
            order by id limit v_lim - v_n) o;
    v_rows := v_rows || v_part;
    v_next := coalesce(v_max, v_next);
  end if;
  select revision into v_rev from public.game_state where game_uuid = p_game_uuid;
  return jsonb_build_object('operations', v_rows, 'next_cursor', v_next, 'revision', coalesce(v_rev, 0));
end;
$$;
revoke all on function public.fetch_operations(uuid, bigint, int) from anon, authenticated;
revoke execute on function public.fetch_operations(uuid, bigint, int) from public;
grant execute on function public.fetch_operations(uuid, bigint, int) to authenticated;

-- ---------------------------------------------------------------------------
-- 6. Compaction
-- ---------------------------------------------------------------------------
-- Archives whole 1,000-op segments from the longest id-ordered prefix of the
-- game's live operations that are all (a) older than 7 days by created_at,
-- (b) before the game's 2,000th-newest operation in canonical order, and
-- (c) before the oldest fold snapshot's watermark. Skips a game whose fold
-- is stale or missing. game_state and the fold are not touched: archiving
-- does not change history, only where it is stored. Returns ops archived.
create or replace function public._evolved_compact_game(p_game uuid)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  v_user uuid;
  v_stale boolean;
  v_bound record;
  v_snap record;
  v_ids bigint[];
  v_first bigint;
  v_last bigint;
  v_count int;
  v_c int;
  v_done int := 0;
begin
  select user_id into v_user from public.players where game_uuid = p_game for update;
  if not found then
    return 0;
  end if;
  select stale into v_stale from public.game_fold where game_uuid = p_game;
  if not found or v_stale then
    return 0;
  end if;
  -- Every archived op is older than every live one, so the 2,000th-newest
  -- operation is always a live row.
  select lamport, device_id, device_seq, op_id into v_bound
    from public.game_operations where game_uuid = p_game
   order by lamport desc, device_id desc, device_seq desc, op_id desc
  offset 1999 limit 1;
  if not found then
    return 0;
  end if;
  select wm_lamport, wm_device, wm_seq, wm_op into v_snap
    from public.game_fold_snapshots where game_uuid = p_game
   order by folded_ops limit 1;
  if not found then
    return 0;
  end if;

  select coalesce(array_agg(id order by id), '{}') into v_ids
    from (select id, bool_and(ok) over (order by id rows unbounded preceding) as prefix_ok
            from (select id,
                         created_at < now() - interval '7 days'
                         and (lamport, device_id, device_seq, op_id)
                             < (v_bound.lamport, v_bound.device_id, v_bound.device_seq, v_bound.op_id)
                         and (lamport, device_id, device_seq, op_id)
                             < (v_snap.wm_lamport, v_snap.wm_device, v_snap.wm_seq, v_snap.wm_op)
                           as ok
                    from public.game_operations where game_uuid = p_game) q) w
   where prefix_ok;

  for v_c in 0 .. coalesce(array_length(v_ids, 1), 0) / 1000 - 1 loop
    v_first := v_ids[v_c * 1000 + 1];
    v_last := v_ids[v_c * 1000 + 1000];
    insert into public.game_operation_segments(game_uuid, user_id, first_id, last_id,
                                               op_count, preset_count, ops)
    select p_game, v_user, v_first, v_last, count(*),
           count(*) filter (where o.kind = 'catchup_preset'),
           jsonb_agg(to_jsonb(o) order by o.id)
      from public.game_operations o
     where o.game_uuid = p_game and o.id between v_first and v_last
    returning op_count into v_count;
    if v_count <> 1000 then
      raise exception 'compaction_segment_size:% game %', v_count, p_game;
    end if;
    insert into public.game_compaction_marks(game_uuid, device_id, user_id, max_seq)
    select p_game, device_id, v_user, max(device_seq)
      from public.game_operations
     where game_uuid = p_game and id between v_first and v_last
     group by device_id
    on conflict (game_uuid, device_id) do update set
      max_seq = greatest(public.game_compaction_marks.max_seq, excluded.max_seq);
    delete from public.game_operations
     where game_uuid = p_game and id between v_first and v_last;
    v_done := v_done + v_count;
  end loop;
  return v_done;
end;
$$;

-- Daily: up to p_max_games games that have at least 1,000 week-old
-- operations and 3,000 live ones (the newest 2,000 always stay live).
create or replace function public._evolved_compact_all(p_max_games int default 20)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  v_g uuid;
  v_done int := 0;
begin
  for v_g in
    select o.game_uuid
      from public.game_operations o
      join public.game_fold f on f.game_uuid = o.game_uuid and not f.stale
     group by o.game_uuid
    having count(*) >= 3000
       and count(*) filter (where o.created_at < now() - interval '7 days') >= 1000
     order by count(*) desc
     limit greatest(coalesce(p_max_games, 20), 1)
  loop
    v_done := v_done + public._evolved_compact_game(v_g);
  end loop;
  return v_done;
end;
$$;

do $$
begin
  if exists (select 1 from cron.job where jobname = 'ankiscape-compaction') then
    perform cron.unschedule('ankiscape-compaction');
  end if;
  perform cron.schedule('ankiscape-compaction', '30 9 * * *',
                        'select public._evolved_compact_all(20)');
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. ops_health: the 0017 numbers plus the archive. live_ops and
--    largest_game_ops stay live-only (the nightly's 20,000 limit is about
--    live rows: past it, compaction is not keeping up).
-- ---------------------------------------------------------------------------
create or replace function public.ops_health()
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'db_bytes', pg_database_size(current_database()),
    'live_ops', (select count(*) from public.game_operations),
    'largest_game_ops', (select coalesce(max(n), 0) from
                           (select count(*) as n from public.game_operations
                             group by game_uuid) s),
    'archived_ops', (select coalesce(sum(op_count), 0) from public.game_operation_segments),
    'segments', (select count(*) from public.game_operation_segments),
    'archive_bytes', pg_total_relation_size('public.game_operation_segments'),
    'games', (select count(*) from public.game_state),
    'stale_folds', (select count(*) from public.game_fold where stale),
    'oldest_stale_since', (select min(stale_since) from public.game_fold where stale),
    'missing_folds', (select count(*) from public.game_state s
                       where not exists (select 1 from public.game_fold f
                                          where f.game_uuid = s.game_uuid)
                         and (exists (select 1 from public.game_operations o
                                       where o.game_uuid = s.game_uuid)
                              or exists (select 1 from public.game_operation_segments g
                                          where g.game_uuid = s.game_uuid))),
    'cron_failures_24h', (select count(*) from cron.job_run_details
                           where status = 'failed' and start_time > now() - interval '1 day'))
$$;

do $$
declare
  v_fn text;
begin
  foreach v_fn in array array[
    'public._evolved_total_ops(uuid)',
    'public._evolved_archived_op(uuid, text, bigint)',
    'public._evolved_presets(uuid)',
    'public._evolved_collection_diagnostics(uuid)',
    'public._evolved_resolve_claims(uuid)',
    'public._evolved_fold_rebuild(uuid, boolean)',
    'public._evolved_compact_game(uuid)',
    'public._evolved_compact_all(int)',
    'public.ops_health()']
  loop
    execute format('revoke all on function %s from public, anon, authenticated', v_fn);
  end loop;
end;
$$;
grant execute on function public.ops_health() to service_role;
