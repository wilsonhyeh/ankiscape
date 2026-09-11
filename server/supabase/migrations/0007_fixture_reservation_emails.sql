-- 0007_fixture_reservation_emails.sql - Suite-owned reservation emails.
-- Append-only fix for 0006: a reserved fixture name must be claimable by its
-- suite-owned address (and only by it) while unclaimed. Without this, the
-- reservation blocked the fixture's own signup.

alter table public.fixture_registry
  add column if not exists reserved_email text not null default '';

create or replace function public._claim_username_before()
returns trigger
language plpgsql security definer
set search_path = public
as $$
declare
  v_norm text;
  v_reserved public.fixture_registry%rowtype;
begin
  v_norm := nullif(trim(both from lower(NEW.raw_user_meta_data ->> 'username_norm')), '');
  if v_norm is null then
    return NEW;  -- non-AnkiScape signup; untouched
  end if;
  if v_norm !~ '^[a-z0-9_]{3,20}$' then
    raise exception 'invalid_username' using errcode = '22000';
  end if;
  if exists (select 1 from public.players p where p.username_norm = v_norm) then
    raise exception 'username_taken' using errcode = '23505';
  end if;
  select * into v_reserved from public.fixture_registry r
   where r.username_norm = v_norm and r.user_id is null;
  if found then
    -- Reserved names are claimable only by the suite-owned address.
    if v_reserved.reserved_email = '' or
       lower(coalesce(NEW.email, '')) <> lower(v_reserved.reserved_email) then
      raise exception 'username_taken' using errcode = '23505';
    end if;
  end if;
  return NEW;
end;
$$;
revoke all on function public._claim_username_before() from public, anon, authenticated;
