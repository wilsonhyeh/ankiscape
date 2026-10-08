-- 0015_checkpoint_retention.sql - keep one checkpoint per game.
-- Append-only migration: never edit 0001-0014 as a rollout strategy.
--
-- submit_operations (0004) inserted the full replay state into
-- public.game_checkpoints on every accepted submit and never removed one.
-- The state carries review_outcomes, which grows with the game's history, so
-- each row grew and one more was written per upload. Nothing reads the table:
-- current state lives in public.game_state, and the only other references are
-- the account-deletion trigger (0010) and test fixtures. On 2026-10-08 it was
-- 406 MB of a 443 MB production database against the Free plan's 500 MB.
--
-- This re-issues submit_operations with the 0004 body unchanged except that,
-- after writing the new checkpoint, it deletes that game's older revisions.
-- The 0004 body was verified identical to production's live definition before
-- this was written. It then deletes every checkpoint that is not its game's
-- latest. Same signature, grants and return shape; no table, column, trigger
-- or capability change.
--
-- Deleted rows only become reusable space after VACUUM, and the file only
-- shrinks after VACUUM FULL, which cannot run inside a migration. After
-- applying on production run, by hand:
--   vacuum (full, analyze) public.game_checkpoints;

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
      -- Keep only the latest checkpoint per game (0015).
      delete from public.game_checkpoints
       where game_uuid = p_game_uuid and revision < coalesce(v_rev, 0) + 1;
    end if;
  end if;

  return jsonb_build_object('accepted', to_jsonb(v_accepted),
                            'conflicts', to_jsonb(v_conflicts),
                            'applied', v_new);
end;
$$;
revoke all on function public.submit_operations(uuid, jsonb) from anon, authenticated;
grant execute on function public.submit_operations(uuid, jsonb) to authenticated;

-- One-time prune: keep each game's latest checkpoint.
delete from public.game_checkpoints k
 where exists (select 1 from public.game_checkpoints n
                where n.game_uuid = k.game_uuid and n.revision > k.revision);
