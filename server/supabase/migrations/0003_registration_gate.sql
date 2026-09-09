-- 0003_registration_gate.sql - Username claim trigger + verified-email gate.
-- Registration stores validated username metadata through this constrained
-- trigger; duplicate username/email races fail the signup itself (BEFORE
-- trigger aborts the auth.users insert), so no orphaned public profiles.
-- Email verification is required before online game submission: mutating
-- game RPCs check auth.users.email_confirmed_at, not just a live session.

create or replace function public._assert_verified(p_user uuid)
returns void
language plpgsql security definer
set search_path = public
as $$
declare
  v_confirmed timestamptz;
begin
  select u.email_confirmed_at into v_confirmed
    from auth.users u where u.id = p_user;
  if v_confirmed is null then
    raise exception 'unverified' using errcode = '28000';
  end if;
end;
$$;
revoke all on function public._assert_verified(uuid) from anon, authenticated;

create or replace function public._claim_username_before()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
  v_norm text;
begin
  v_norm := nullif(trim(both from lower(NEW.raw_user_meta_data ->> 'username_norm')), '');
  if v_norm is null then
    return NEW;  -- non-AnkiScape signup; untouched
  end if;
  if v_norm !~ '^[a-z0-9_]{3,20}$' then
    raise exception 'invalid_username' using errcode = '22000';
  end if;
  if exists (select 1 from public.players p where p.username_norm = v_norm) then
    -- Abort the signup itself: no auth.users row, no orphaned profile.
    raise exception 'username_taken' using errcode = '23505';
  end if;
  return NEW;
end;
$$;
revoke all on function public._claim_username_before() from anon, authenticated;

create or replace function public._claim_username_after()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
  v_norm text;
  v_display text;
begin
  v_norm := nullif(trim(both from lower(NEW.raw_user_meta_data ->> 'username_norm')), '');
  if v_norm is null then
    return NEW;
  end if;
  v_display := coalesce(nullif(NEW.raw_user_meta_data ->> 'username_display', ''), v_norm);
  insert into public.players(user_id, username_norm, username_display)
  values (NEW.id, v_norm, left(v_display, 64));
  return NEW;
end;
$$;
revoke all on function public._claim_username_after() from anon, authenticated;

drop trigger if exists anikiscape_claim_username_before on auth.users;
create trigger anikiscape_claim_username_before
  before insert on auth.users
  for each row execute function public._claim_username_before();

drop trigger if exists anikiscape_claim_username_after on auth.users;
create trigger anikiscape_claim_username_after
  after insert on auth.users
  for each row execute function public._claim_username_after();

-- Gate the mutating game RPCs on verified email (full bodies re-issued).
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
  perform public._assert_verified(v_user);
  if p_game_uuid is null then
    raise exception 'invalid_game' using errcode = '22000';
  end if;
  select * into v_row from public.players where user_id = v_user for update;
  if not found then
    raise exception 'no_profile' using errcode = '28000';
  end if;
  if v_row.game_uuid is null then
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
  perform public._assert_verified(v_user);
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
