-- 0008_account_lifecycle.sql - Account status lookup + rate-limit buckets.
-- Append-only migration: never edit 0001-0007 as a rollout strategy.
--
-- Adds:
--   * public.account_lifecycle_status(email, username) - service-role-only
--     helper returning {email_status: new|unconfirmed|confirmed,
--     username_available: bool}. It reads auth.users but returns no ids,
--     stored emails, existing usernames or metadata.
--   * public.account_status_buckets + public.account_status_consume(...) -
--     atomic fixed-window rate-limit buckets keyed by HMAC digests supplied
--     by the account-status Edge Function (raw emails never reach SQL).
--
-- Both helpers are revoked from public/anon/authenticated and granted only
-- to service_role. No client role can read auth.users or enumerate emails.

-- ---------------------------------------------------------------------------
-- 1. Email/username status helper (service role only).
-- ---------------------------------------------------------------------------
create or replace function public.account_lifecycle_status(
  p_email text,
  p_username text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_email text := lower(trim(coalesce(p_email, '')));
  v_username text := lower(trim(coalesce(p_username, '')));
  v_status text := 'new';
  v_available boolean := true;
  v_confirmed timestamptz;
begin
  if v_email = '' or length(v_email) > 320 or position('@' in v_email) = 0 then
    raise exception 'invalid_email' using errcode = '22000';
  end if;
  if v_username <> '' then
    if v_username !~ '^[a-z0-9_]{3,20}$' then
      raise exception 'invalid_username' using errcode = '22000';
    end if;
    -- A username is unavailable when any player row (real or fixture) holds
    -- it, or when a reserved fixture name is being kept free for its suite.
    select not (
      exists (select 1 from public.players p where p.username_norm = v_username)
      or exists (select 1 from public.fixture_registry r
                 where r.username_norm = v_username)
    ) into v_available;
  end if;
  -- Email matching is trimmed, case-insensitive, consistent with the
  -- configured Auth provider. Plus tags and dots are preserved (never
  -- normalized away): Auth's own uniqueness stays the final authority.
  select u.email_confirmed_at into v_confirmed
    from auth.users u
   where lower(trim(u.email)) = v_email
   order by u.created_at desc
   limit 1;
  if found then
    v_status := case when v_confirmed is null then 'unconfirmed'
                     else 'confirmed' end;
  end if;
  return jsonb_build_object(
    'email_status', v_status,
    'username_available', v_available);
end;
$$;
revoke all on function public.account_lifecycle_status(text, text)
  from public, anon, authenticated;
grant execute on function public.account_lifecycle_status(text, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- 2. Atomic fixed-window rate-limit buckets.
-- ---------------------------------------------------------------------------
create table if not exists public.account_status_buckets (
  bucket_key text primary key,
  window_start timestamptz not null default now(),
  hits int not null default 0,
  expires_at timestamptz not null
);
create index if not exists account_status_buckets_expiry_ix
  on public.account_status_buckets(expires_at);

alter table public.account_status_buckets enable row level security;
revoke all on public.account_status_buckets from public, anon, authenticated;

-- One call = one hit. Returns {allowed, remaining, reset_s}. Keys are HMAC
-- digests computed inside the Edge Function with a server-only secret, so
-- this table never stores raw emails or usernames.
create or replace function public.account_status_consume(
  p_key text,
  p_limit int,
  p_window_s int default 60
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_now timestamptz := now();
  v_window int := greatest(least(coalesce(p_window_s, 60), 3600), 1);
  v_limit int := greatest(least(coalesce(p_limit, 10), 100000), 1);
  v_row public.account_status_buckets%rowtype;
  v_reset int;
begin
  if p_key is null or length(p_key) < 8 or length(p_key) > 200 then
    raise exception 'invalid_bucket_key' using errcode = '22000';
  end if;
  -- Opportunistic sweep keeps the table bounded without a scheduler.
  delete from public.account_status_buckets where expires_at < v_now;
  insert into public.account_status_buckets(bucket_key, window_start, hits,
                                            expires_at)
  values (p_key, v_now, 1, v_now + make_interval(secs => v_window))
  on conflict (bucket_key) do update
     set hits = case
           when public.account_status_buckets.window_start
                + make_interval(secs => v_window) <= v_now
             then 1
           else public.account_status_buckets.hits + 1
         end,
         window_start = case
           when public.account_status_buckets.window_start
                + make_interval(secs => v_window) <= v_now
             then v_now
           else public.account_status_buckets.window_start
         end,
         expires_at = case
           when public.account_status_buckets.window_start
                + make_interval(secs => v_window) <= v_now
             then v_now + make_interval(secs => v_window)
           else public.account_status_buckets.expires_at
         end
  returning * into v_row;
  v_reset := greatest(
    0,
    ceil(extract(epoch from
      (v_row.window_start + make_interval(secs => v_window)) - v_now))::int);
  return jsonb_build_object(
    'allowed', v_row.hits <= v_limit,
    'remaining', greatest(v_limit - v_row.hits, 0),
    'reset_s', v_reset);
end;
$$;
revoke all on function public.account_status_consume(text, int, int)
  from public, anon, authenticated;
grant execute on function public.account_status_consume(text, int, int)
  to service_role;
