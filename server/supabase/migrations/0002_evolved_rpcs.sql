-- 0002_evolved_rpcs.sql - Authenticated RPCs (contract F).
-- Mutating RPCs derive ownership from auth.uid(), never body user_id.
-- Lock player game row (SELECT ... FOR UPDATE) for insert/validate/replay in
-- one transaction; acknowledge only after commit.

-- link_game: bind an unclaimed game to the caller, or resume the same binding.
create or replace function public.link_game(p_game_uuid uuid)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_row public.players%rowtype;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  if p_game_uuid is null then
    raise exception 'invalid_game' using errcode = '22000';
  end if;
  select * into v_row from public.players where user_id = v_user for update;
  if not found then
    raise exception 'no_profile' using errcode = '28000';
  end if;
  if v_row.game_uuid is null then
    -- Claim only if no other player holds this game.
    if exists (select 1 from public.players where game_uuid = p_game_uuid) then
      raise exception 'game_claimed' using errcode = '23505';
    end if;
    update public.players set game_uuid = p_game_uuid, updated_at = now()
      where user_id = v_user;
    insert into public.game_state(game_uuid, user_id) values (p_game_uuid, v_user)
      on conflict (game_uuid) do nothing;
    return jsonb_build_object('game_uuid', p_game_uuid, 'resumed', false);
  elsif v_row.game_uuid = p_game_uuid then
    return jsonb_build_object('game_uuid', p_game_uuid, 'resumed', true);
  else
    raise exception 'game_mismatch' using errcode = '23505';
  end if;
end;
$$;
revoke all on function public.link_game(uuid) from anon, authenticated;
grant execute on function public.link_game(uuid) to authenticated;

-- submit_operations: max 200 ops / 256 KiB per request. No xp_delta or
-- client total parameter. Exact retries return same acceptance; reused IDs
-- with changed content return conflict.
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
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
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
  for v_op in select * from jsonb_array_elements(p_ops) loop
    begin
      insert into public.game_operations(
        op_id, game_uuid, user_id, device_id, device_seq, lamport, kind, payload, payload_hash)
      values (
        nullif(v_op->>'op_id','')::uuid, p_game_uuid, v_user,
        v_op->>'device_id', (v_op->>'device_seq')::bigint, (v_op->>'lamport')::bigint,
        v_op->>'kind', coalesce(v_op->'payload','{}'::jsonb),
        encode(extensions.digest(coalesce(v_op->'payload','{}'::jsonb)::text::bytea, 'sha256'), 'hex'));
      v_accepted := v_accepted || (nullif(v_op->>'op_id','')::uuid);
    exception when unique_violation then
      -- Same op_id already stored: same content -> same acceptance result;
      -- changed content -> conflict (checked by payload hash).
      if exists (select 1 from public.game_operations o
                 where o.op_id = nullif(v_op->>'op_id','')::uuid
                   and o.payload = coalesce(v_op->'payload','{}'::jsonb)) then
        v_accepted := v_accepted || (nullif(v_op->>'op_id','')::uuid);
      else
        v_conflicts := v_conflicts || jsonb_build_object('op_id', v_op->>'op_id', 'error', 'id_conflict');
      end if;
    end;
  end loop;
  update public.game_state set revision = revision + 1, updated_at = now()
    where game_uuid = p_game_uuid;
  return jsonb_build_object('accepted', to_jsonb(v_accepted), 'conflicts', to_jsonb(v_conflicts));
end;
$$;
revoke all on function public.submit_operations(uuid, jsonb) from anon, authenticated;
grant execute on function public.submit_operations(uuid, jsonb) to authenticated;

-- fetch_operations: cursor-based pages (cursor = game_operations.id), max 200.
create or replace function public.fetch_operations(p_game_uuid uuid, p_cursor bigint, p_limit int)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_owner uuid;
  v_lim int := least(greatest(coalesce(p_limit, 100), 1), 200);
  v_rows jsonb;
  v_next bigint;
  v_rev bigint;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select user_id into v_owner from public.players where game_uuid = p_game_uuid;
  if not found or v_owner != v_user then
    raise exception 'game_mismatch' using errcode = '42501';
  end if;
  select coalesce(jsonb_agg(to_jsonb(o) order by o.id), '[]'::jsonb), coalesce(max(o.id), coalesce(p_cursor, 0))
    into v_rows, v_next
    from (select * from public.game_operations
          where game_uuid = p_game_uuid and id > coalesce(p_cursor, 0)
          order by id limit v_lim) o;
  select revision into v_rev from public.game_state where game_uuid = p_game_uuid;
  return jsonb_build_object('operations', v_rows, 'next_cursor', v_next, 'revision', coalesce(v_rev, 0));
end;
$$;
revoke all on function public.fetch_operations(uuid, bigint, int) from anon, authenticated;
grant execute on function public.fetch_operations(uuid, bigint, int) to authenticated;

-- get_game_state: private linked-game snapshot/revision.
create or replace function public.get_game_state(p_game_uuid uuid)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_user uuid := auth.uid();
  v_owner uuid;
  v_state jsonb;
begin
  if v_user is null then
    raise exception 'not_authenticated' using errcode = '28000';
  end if;
  select user_id into v_owner from public.players where game_uuid = p_game_uuid;
  if not found or v_owner != v_user then
    raise exception 'game_mismatch' using errcode = '42501';
  end if;
  select to_jsonb(s) into v_state from public.game_state s where s.game_uuid = p_game_uuid;
  if v_state is null then
    raise exception 'no_state' using errcode = '02000';
  end if;
  return v_state;
end;
$$;
revoke all on function public.get_game_state(uuid) from anon, authenticated;
grant execute on function public.get_game_state(uuid) to authenticated;

-- hiscores: allowlisted skill, limit 1..100, XP desc / username / user_id.
-- Rank uses competition ranking; secondary sort is display only.
create or replace function public.hiscores(p_skill text, p_limit int)
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
  select coalesce(jsonb_agg(t order by t.xp desc, t.username_norm, t.user_id), '[]'::jsonb)
    into v_rows from (
      select p.username_display as username, p.username_norm, p.user_id,
             coalesce(((s.xp ->> p_skill)::bigint), 0) as xp,
             rank() over (order by coalesce(((s.xp ->> p_skill)::bigint), 0) desc) as rank
      from public.players p join public.game_state s on s.game_uuid = p.game_uuid
      where p.status = 'active' and p.game_uuid is not null
      order by xp desc, p.username_norm, p.user_id
      limit v_lim) t;
  return v_rows;
end;
$$;
revoke all on function public.hiscores(text, int) from anon, authenticated;
grant execute on function public.hiscores(text, int) to anon, authenticated;

-- public_profile: safe username + six skills with level/xp/rank. No email.
create or replace function public.public_profile(p_username_norm text)
returns jsonb
language plpgsql security definer
set search_path = public
as $$
declare
  v_state jsonb;
  v_name text;
begin
  select p.username_display, to_jsonb(s) into v_name, v_state
  from public.players p join public.game_state s on s.game_uuid = p.game_uuid
  where p.username_norm = lower(trim(p_username_norm)) and p.status = 'active';
  if v_state is null then
    raise exception 'no_profile' using errcode = '02000';
  end if;
  return jsonb_build_object('username', v_name, 'state', v_state);
end;
$$;
revoke all on function public.public_profile(text) from anon, authenticated;
grant execute on function public.public_profile(text) to anon, authenticated;
