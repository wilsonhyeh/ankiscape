-- 0017_incremental_fold.sql - score each upload from a stored fold instead of
-- replaying the game's whole history (plan: HQ
-- plans/ankiscape-incremental-scoring-and-compaction.md, Phase 1).
-- Append-only migration: never edit 0001-0016 as a rollout strategy.
-- Re-runnable: every object is created idempotently.
--
-- Why: submit_operations replayed the entire history on every upload. 0014
-- made that linear, but production still measured 0.26-0.5 ms per operation,
-- so a game reaches the authenticated role's 8 s statement_timeout again at
-- roughly 15,000-30,000 operations.
--
-- What:
--   * game_fold          - per game: the fold state (evolved_fold_state from
--                          0016) at a canonical watermark, its diagnostics,
--                          and a stale flag.
--   * game_fold_snapshots- the fold state every 100 folded operations, newest
--                          20 kept (>= 1,900 operations of rewind; the deepest
--                          undo seen on production was 59).
--   * game_review_keys   - per review key: status and winning claim.
--   * submit_operations  - tries _evolved_fold_fast (cost ~ the batch); on
--                          anything it cannot prove equal to a full replay it
--                          rebuilds synchronously for games of <= 3,000
--                          operations, otherwise marks the fold stale.
--   * pg_cron            - rebuilds stale folds every minute, as postgres (no
--                          statement_timeout); purges its own run history.
--   * game_checkpoints   - no longer written (nothing reads it).
--   * ops_health()       - service_role-only capacity numbers for the nightly.
-- Invariant: game_state always equals what evolved_replay (0016) computes,
-- except while a fold is stale (then it keeps the previous values until the
-- background job runs). dev/incremental_parity.py checks this.

-- ---------------------------------------------------------------------------
-- 1. Tables
-- ---------------------------------------------------------------------------
create table if not exists public.game_fold (
  game_uuid uuid primary key,
  user_id uuid not null references public.players(user_id) on delete cascade,
  rules_version int not null,
  wm_lamport bigint,
  wm_device text,
  wm_seq bigint,
  wm_op uuid,
  folded_ops bigint not null default 0,
  state public.evolved_fold_state not null,
  diagnostics text[] not null default '{}',
  stale boolean not null default false,
  stale_since timestamptz,
  rebuilt_at timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists game_fold_stale_ix on public.game_fold(stale_since) where stale;

create table if not exists public.game_fold_snapshots (
  game_uuid uuid not null,
  user_id uuid not null references public.players(user_id) on delete cascade,
  folded_ops bigint not null,
  wm_lamport bigint not null,
  wm_device text not null,
  wm_seq bigint not null,
  wm_op uuid not null,
  state public.evolved_fold_state not null,
  primary key (game_uuid, folded_ops)
);

create table if not exists public.game_review_keys (
  game_uuid uuid not null,
  review_key text not null,
  user_id uuid not null references public.players(user_id) on delete cascade,
  status text not null check (status in ('active','retracted','skipped','none')),
  provenance text check (provenance in ('direct','catchup')),
  winner_op uuid,
  w_lamport bigint,
  w_device text,
  w_seq bigint,
  -- exactly one award and no skip claim (see _evolved_resolve_claims)
  simple boolean not null default true,
  primary key (game_uuid, review_key)
);

-- Catch-up preset lookup must not scan the whole history.
create index if not exists game_ops_preset_ix
  on public.game_operations(game_uuid, lamport, device_id, device_seq, op_id)
  where kind = 'catchup_preset';

alter table public.game_fold enable row level security;
alter table public.game_fold_snapshots enable row level security;
alter table public.game_review_keys enable row level security;
revoke all on public.game_fold from anon, authenticated;
revoke all on public.game_fold_snapshots from anon, authenticated;
revoke all on public.game_review_keys from anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. Writing game_state from a fold
-- ---------------------------------------------------------------------------
create or replace function public._evolved_state_write(
  p_game uuid, p_state public.evolved_fold_state, p_diagnostics text[],
  p_bump boolean)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_j jsonb := public._evolved_state_json(p_state);
begin
  update public.game_state set
    revision = revision + case when p_bump then 1 else 0 end,
    xp = coalesce(v_j->'xp_micro', '{}'::jsonb),
    inventory = coalesce(v_j->'inventory', '{}'::jsonb),
    counters = coalesce(v_j->'counters', '{}'::jsonb),
    achievements = coalesce(v_j->'achievements', '[]'::jsonb),
    checkpoint = jsonb_build_object(
      'levels', coalesce(v_j->'levels', '{}'::jsonb),
      'diagnostics', to_jsonb(coalesce(p_diagnostics, '{}'::text[]))),
    updated_at = now()
  where game_uuid = p_game;
end;
$$;

create or replace function public._evolved_fold_snapshot(
  p_game uuid, p_user uuid, p_n bigint, p_lamport bigint, p_device text,
  p_seq bigint, p_op uuid, p_state public.evolved_fold_state)
returns void
language sql
security definer
set search_path = public
as $$
  insert into public.game_fold_snapshots(game_uuid, user_id, folded_ops, wm_lamport,
                                          wm_device, wm_seq, wm_op, state)
  values (p_game, p_user, p_n, p_lamport, p_device, p_seq, p_op, p_state)
  on conflict (game_uuid, folded_ops) do update set
    wm_lamport = excluded.wm_lamport, wm_device = excluded.wm_device,
    wm_seq = excluded.wm_seq, wm_op = excluded.wm_op, state = excluded.state
$$;

-- ---------------------------------------------------------------------------
-- 3. Full rebuild: keys + fold + snapshots + game_state from the history.
--    Same claim resolution and apply step as evolved_replay (0016).
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
    from public.game_operations o
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
  select count(*) into v_total from public.game_operations where game_uuid = p_game;

  for v_op in
    select op_id, kind, payload, lamport, device_id, device_seq
      from public.game_operations
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
-- 4. Fast path: fold only this upload's operations onto the stored fold.
--    Raises fold_fallback (SQLSTATE AS001) whenever it cannot guarantee the
--    result equals a full replay; the caller's sub-block then discards every
--    write made here.
-- ---------------------------------------------------------------------------
create or replace function public._evolved_fold_fast(p_game uuid, p_new_ids uuid[])
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_rules jsonb := public.evolved_rules();
  v_thresholds bigint[] := public._evolved_thresholds(v_rules);
  v_rv int := coalesce((v_rules->>'rules_version')::int, 1);
  v_fold public.game_fold%rowtype;
  v_state public.evolved_fold_state;
  v_applied record;
  v_n bigint;
  v_wl bigint; v_wd text; v_ws bigint; v_wo uuid;
  v_sl bigint; v_sd text; v_ss bigint; v_so uuid;
  v_ts bigint[];
  v_sk text[];
  v_op record;
  v_r record;
  v_rk text;
  v_k public.game_review_keys%rowtype;
  v_k2 record;
  v_new_status text;
  v_snap public.game_fold_snapshots%rowtype;
  v_skill text;
  v_keep bigint;
begin
  select * into v_fold from public.game_fold where game_uuid = p_game for update;
  if not found or v_fold.stale or v_fold.rules_version <> v_rv then
    raise exception 'fold_fallback' using errcode = 'AS001';
  end if;
  if exists (select 1 from public.game_operations
              where op_id = any(p_new_ids)
                and kind not in ('review_award','review_skip','review_retract','review_restore')) then
    raise exception 'fold_fallback' using errcode = 'AS001';   -- presets re-time history
  end if;

  select o_ts, o_skill into v_ts, v_sk from public._evolved_presets(p_game);
  v_state := v_fold.state;
  v_n := v_fold.folded_ops;
  v_wl := v_fold.wm_lamport; v_wd := v_fold.wm_device;
  v_ws := v_fold.wm_seq; v_wo := v_fold.wm_op;

  for v_op in
    select op_id, kind, payload, lamport, device_id, device_seq
      from public.game_operations
     where op_id = any(p_new_ids)
     order by lamport, device_id, device_seq, op_id
  loop
    -- A late insertion changes the order of history already folded.
    if v_wo is not null and (v_op.lamport, v_op.device_id, v_op.device_seq, v_op.op_id)
                            <= (v_wl, v_wd, v_ws, v_wo) then
      raise exception 'fold_fallback' using errcode = 'AS001';
    end if;

    if v_op.kind in ('review_award', 'review_skip') then
      v_rk := v_op.payload->>'review_key';
      if coalesce(v_rk, '') = '' then
        raise exception 'fold_fallback' using errcode = 'AS001';
      end if;
      perform 1 from public.game_review_keys where game_uuid = p_game and review_key = v_rk;
      if found then
        -- a second claim for a known key: precedence may change history
        raise exception 'fold_fallback' using errcode = 'AS001';
      end if;
      if v_op.kind = 'review_award' then
        insert into public.game_review_keys(game_uuid, review_key, user_id, status, provenance,
                                            winner_op, w_lamport, w_device, w_seq)
        values (p_game, v_rk, v_fold.user_id, 'active',
                case when coalesce(v_op.payload->>'provenance', 'catchup') = 'direct'
                     then 'direct' else 'catchup' end,
                v_op.op_id, v_op.lamport, v_op.device_id, v_op.device_seq);
        v_skill := null;
        if coalesce(v_op.payload->>'provenance', 'catchup') <> 'direct' then
          v_skill := public._evolved_preset_skill(
            v_ts, v_sk, coalesce((v_op.payload->>'review_ts')::bigint, 0));
        end if;
        v_applied := public._evolved_apply_winner(v_rules, v_thresholds, v_state,
          p_game::text, v_rk, v_op.payload, v_skill);
        v_state := v_applied.o_state;
      else
        insert into public.game_review_keys(game_uuid, review_key, user_id, status)
        values (p_game, v_rk, v_fold.user_id, 'skipped');
      end if;
    else
      -- review_retract / review_restore
      v_rk := v_op.payload->>'target_review_key';
      if coalesce(v_rk, '') = '' then
        raise exception 'fold_fallback' using errcode = 'AS001';
      end if;
      select * into v_k from public.game_review_keys
       where game_uuid = p_game and review_key = v_rk for update;
      -- Undo/redo of a key with a duplicate or skip claim changes its
      -- diagnostics; only simple keys stay on the fast path.
      if not found or v_k.status not in ('active', 'retracted') or v_k.winner_op is null
         or not v_k.simple then
        raise exception 'fold_fallback' using errcode = 'AS001';
      end if;
      v_new_status := case when v_op.kind = 'review_retract' then 'retracted' else 'active' end;
      if v_new_status <> v_k.status then
        update public.game_review_keys set status = v_new_status
         where game_uuid = p_game and review_key = v_rk;
        -- Rewind to the newest snapshot before the winning claim, then re-fold
        -- every operation up to (not including) this one.
        select * into v_snap from public.game_fold_snapshots s
         where s.game_uuid = p_game
           and (s.wm_lamport, s.wm_device, s.wm_seq, s.wm_op)
               < (v_k.w_lamport, v_k.w_device, v_k.w_seq, v_k.winner_op)
         order by s.folded_ops desc
         limit 1;
        if found then
          v_state := v_snap.state;
          v_n := v_snap.folded_ops;
          v_sl := v_snap.wm_lamport; v_sd := v_snap.wm_device;
          v_ss := v_snap.wm_seq; v_so := v_snap.wm_op;
        elsif v_n <= 2000 then
          -- Small game: no snapshot was ever pruned, so start from empty.
          v_state := public._evolved_fold_empty(v_thresholds);
          v_n := 0;
          -- Below every real tuple (lamport >= 0, device_seq > 0), so the
          -- range query below keeps both index bounds.
          v_sl := -1; v_sd := ''; v_ss := -1;
          v_so := '00000000-0000-0000-0000-000000000000';
        else
          raise exception 'fold_fallback' using errcode = 'AS001';   -- older than the window
        end if;
        delete from public.game_fold_snapshots where game_uuid = p_game and folded_ops > v_n;
        for v_r in
          select op_id, kind, payload, lamport, device_id, device_seq
            from public.game_operations
           where game_uuid = p_game
             and (lamport, device_id, device_seq, op_id) > (v_sl, v_sd, v_ss, v_so)
             and (lamport, device_id, device_seq, op_id)
                 < (v_op.lamport, v_op.device_id, v_op.device_seq, v_op.op_id)
           order by lamport, device_id, device_seq, op_id
        loop
          v_n := v_n + 1;
          if v_r.kind = 'review_award' then
            select status, winner_op into v_k2 from public.game_review_keys
             where game_uuid = p_game and review_key = v_r.payload->>'review_key';
            if found and v_k2.status = 'active' and v_k2.winner_op = v_r.op_id then
              v_skill := null;
              if coalesce(v_r.payload->>'provenance', 'catchup') <> 'direct' then
                v_skill := public._evolved_preset_skill(
                  v_ts, v_sk, coalesce((v_r.payload->>'review_ts')::bigint, 0));
              end if;
              v_applied := public._evolved_apply_winner(v_rules, v_thresholds, v_state,
                p_game::text, v_r.payload->>'review_key', v_r.payload, v_skill);
              v_state := v_applied.o_state;
            end if;
          end if;
          if v_n % 100 = 0 then
            perform public._evolved_fold_snapshot(p_game, v_fold.user_id, v_n, v_r.lamport,
              v_r.device_id, v_r.device_seq, v_r.op_id, v_state);
          end if;
        end loop;
      end if;
    end if;

    v_n := v_n + 1;
    v_wl := v_op.lamport; v_wd := v_op.device_id; v_ws := v_op.device_seq; v_wo := v_op.op_id;
    if v_n % 100 = 0 then
      perform public._evolved_fold_snapshot(p_game, v_fold.user_id, v_n, v_wl, v_wd, v_ws,
                                            v_wo, v_state);
    end if;
  end loop;

  -- Keep the newest 20 snapshots.
  select folded_ops into v_keep from public.game_fold_snapshots
   where game_uuid = p_game order by folded_ops desc offset 19 limit 1;
  if v_keep is not null then
    delete from public.game_fold_snapshots where game_uuid = p_game and folded_ops < v_keep;
  end if;

  update public.game_fold set
    wm_lamport = v_wl, wm_device = v_wd, wm_seq = v_ws, wm_op = v_wo,
    folded_ops = v_n, state = v_state, updated_at = now()
  where game_uuid = p_game;
  perform public._evolved_state_write(p_game, v_state, v_fold.diagnostics, true);
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. submit_operations: the 0015 body; only step 3 changes (and the inserted
--    op ids are collected). game_checkpoints is no longer written.
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
        select count(*) into v_total from public.game_operations where game_uuid = p_game_uuid;
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
-- 6. Background rebuild of stale folds (pg_cron, every minute, as postgres).
-- ---------------------------------------------------------------------------
create or replace function public._evolved_fold_rebuild_stale(p_max int default 5)
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
    select game_uuid from public.game_fold where stale
     order by stale_since nulls first limit greatest(coalesce(p_max, 5), 1)
  loop
    -- The same lock submit_operations takes; an upload in flight wins and
    -- this game is retried on the next run.
    perform 1 from public.players where game_uuid = v_g for update skip locked;
    if not found then
      continue;
    end if;
    perform 1 from public.game_fold where game_uuid = v_g and stale;
    if not found then
      continue;
    end if;
    perform public._evolved_fold_rebuild(v_g, true);
    v_done := v_done + 1;
  end loop;
  return v_done;
end;
$$;

create extension if not exists pg_cron;

do $$
begin
  if exists (select 1 from cron.job where jobname = 'ankiscape-fold-rebuild') then
    perform cron.unschedule('ankiscape-fold-rebuild');
  end if;
  if exists (select 1 from cron.job where jobname = 'ankiscape-cron-history-purge') then
    perform cron.unschedule('ankiscape-cron-history-purge');
  end if;
  perform cron.schedule('ankiscape-fold-rebuild', '* * * * *',
                        'select public._evolved_fold_rebuild_stale(5)');
  -- cron.job_run_details is never cleaned automatically; one row per run of a
  -- once-a-minute job is 1,440 rows a day against the 500 MB Free cap.
  perform cron.schedule('ankiscape-cron-history-purge', '15 3 * * *',
    $q$delete from cron.job_run_details where end_time < now() - interval '7 days'$q$);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. Capacity numbers for the nightly production check (service_role only).
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
    'games', (select count(*) from public.game_state),
    'stale_folds', (select count(*) from public.game_fold where stale),
    'oldest_stale_since', (select min(stale_since) from public.game_fold where stale),
    'missing_folds', (select count(*) from public.game_state s
                       where not exists (select 1 from public.game_fold f
                                          where f.game_uuid = s.game_uuid)
                         and exists (select 1 from public.game_operations o
                                      where o.game_uuid = s.game_uuid)),
    'cron_failures_24h', (select count(*) from cron.job_run_details
                           where status = 'failed' and start_time > now() - interval '1 day'))
$$;

do $$
declare
  v_fn text;
begin
  foreach v_fn in array array[
    'public._evolved_state_write(uuid, public.evolved_fold_state, text[], boolean)',
    'public._evolved_fold_snapshot(uuid, uuid, bigint, bigint, text, bigint, uuid, public.evolved_fold_state)',
    'public._evolved_fold_rebuild(uuid, boolean)',
    'public._evolved_fold_fast(uuid, uuid[])',
    'public._evolved_fold_rebuild_stale(int)',
    'public.ops_health()']
  loop
    execute format('revoke all on function %s from public, anon, authenticated', v_fn);
  end loop;
end;
$$;
grant execute on function public.ops_health() to service_role;

-- ---------------------------------------------------------------------------
-- 8. Backfill: a fold for every game that has operations (no revision bump;
--    game_state already holds these values).
-- ---------------------------------------------------------------------------
do $$
declare
  v_g uuid;
begin
  for v_g in
    select s.game_uuid from public.game_state s
     where exists (select 1 from public.game_operations o where o.game_uuid = s.game_uuid)
       and not exists (select 1 from public.game_fold f where f.game_uuid = s.game_uuid)
  loop
    perform public._evolved_fold_rebuild(v_g, false);
  end loop;
end;
$$;
