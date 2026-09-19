-- server/supabase/tests/0005_public_board.test.sql - Public demo board.
-- Run: cd server && supabase test db
-- Covers migration 0009 (privileged demo flag, allowlisted public ranking and
-- profile responses, demo visibility, filter-before-rank) and migration 0008
-- (service-role-only account lifecycle status + atomic rate buckets).
begin;
select plan(27);

-- ---------------------------------------------------------------------------
-- Fixtures: one demo, one real player, one retired-style test player.
-- ---------------------------------------------------------------------------
insert into public.fixture_registry
  (suite_id, suite_version, username_norm, expected_trace_hash, reserved_email)
values
  ('public-demo', 1, 'demowillow', 'demo-hash-willow',
   'demowillow@public-demo.example.invalid'),
  ('hosted-v1', 1, 'testtom', 'trace-hash-tom',
   'testtom@hosted-v1.example.invalid');

insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values
  ('40000000-0000-4000-8000-000000000001', '', 'authenticated',
   'demowillow@public-demo.example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"demowillow","username_display":"DemoWillow"}'),
  ('40000000-0000-4000-8000-000000000002', '', 'authenticated',
   'realplayer@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"realplayer","username_display":"RealPlayer"}'),
  ('40000000-0000-4000-8000-000000000003', '', 'authenticated',
   'testtom@hosted-v1.example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"testtom","username_display":"TestTom"}'),
  ('40000000-0000-4000-8000-000000000004', '', 'authenticated',
   'confirmed@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(), '{}'),
  ('40000000-0000-4000-8000-000000000005', '', 'authenticated',
   'unconfirmed@example.invalid', crypt('pw123456', gen_salt('bf')),
   null, now(), now(), '{}')
on conflict (id) do nothing;

update public.players set is_demo = true where username_norm = 'demowillow';
update public.players set game_uuid = 'cccccccc-0000-4000-8000-000000000001'
 where username_norm = 'demowillow';
update public.players set game_uuid = 'cccccccc-0000-4000-8000-000000000002'
 where username_norm = 'realplayer';
update public.players set game_uuid = 'cccccccc-0000-4000-8000-000000000003'
 where username_norm = 'testtom';

insert into public.game_state(game_uuid, user_id, xp, revision)
select p.game_uuid, p.user_id,
       jsonb_build_object('mining', case p.username_norm
                                      when 'realplayer' then 9000000
                                      when 'demowillow' then 5000000
                                      else 99000000 end),
       1
  from public.players p
 where p.game_uuid is not null;

-- 1. Demo flag defaults false and is privileged to change.
select is(
  (select is_demo from public.players where username_norm = 'realplayer'),
  false, 'demo flag defaults false for ordinary players');
select set_config('request.jwt.claims',
  '{"sub":"40000000-0000-4000-8000-000000000002","role":"authenticated"}', true);
set local role authenticated;
-- authenticated has zero UPDATE privilege on players (0001 revokes all), so
-- the write is refused before the demo guard trigger can fire; the errcode is
-- the assertion (message pinned only where the trigger is reachable).
select throws_ok(
  $$ update public.players set is_demo = true
      where username_norm = 'realplayer' $$,
  '42501', null,
  'authenticated callers cannot set the demo flag');
reset role;

-- 2. Account lifecycle status is service-role only and returns no identity.
set local role anon;
select throws_ok(
  $$ select public.account_lifecycle_status('confirmed@example.invalid') $$,
  '42501', null, 'anon cannot read account lifecycle status');
reset role;
select set_config('request.jwt.claims',
  '{"sub":"40000000-0000-4000-8000-000000000002","role":"authenticated"}', true);
set local role authenticated;
select throws_ok(
  $$ select public.account_lifecycle_status('confirmed@example.invalid') $$,
  '42501', null, 'authenticated callers cannot read account lifecycle status');
reset role;
select is(
  (select public.account_lifecycle_status('fresh@example.invalid')
          ->> 'email_status'),
  'new', 'unknown email reads as new');
select is(
  (select public.account_lifecycle_status('confirmed@example.invalid')
          ->> 'email_status'),
  'confirmed', 'confirmed email reads as confirmed');
select is(
  (select public.account_lifecycle_status('unconfirmed@example.invalid')
          ->> 'email_status'),
  'unconfirmed', 'unconfirmed email reads as unconfirmed');
select is(
  (select public.account_lifecycle_status('confirmed@example.invalid',
                                          'realplayer')
          ->> 'username_available'),
  'false', 'taken username is unavailable');
select is(
  (select public.account_lifecycle_status('confirmed@example.invalid',
                                          'brandnew_name')
          ->> 'username_available'),
  'true', 'free username is available');
select is(
  (select public.account_lifecycle_status('confirmed@example.invalid',
                                          'demowillow')
          ->> 'username_available'),
  'false', 'reserved registry name is unavailable');
select throws_ok(
  $$ select public.account_lifecycle_status('not-an-email') $$,
  '22000', 'invalid_email', 'malformed email is rejected');

-- 3. Atomic rate buckets.
select ok(
  (select (public.account_status_consume('bucket-test-a', 2) ->> 'allowed')
          ::boolean),
  'first hit is allowed');
select ok(
  (select (public.account_status_consume('bucket-test-a', 2) ->> 'allowed')
          ::boolean),
  'second hit within the limit is allowed');
select ok(
  not (select (public.account_status_consume('bucket-test-a', 2)
               ->> 'allowed')::boolean),
  'third hit over the limit is refused');
select throws_ok(
  $$ select public.account_status_consume('short', 2) $$,
  '22000', 'invalid_bucket_key', 'malformed bucket keys are rejected');

-- 4. Public ranking contract (anon).
set local role anon;
select is(
  (select array_agg(k order by k)::text[] from jsonb_object_keys(
     (select r from jsonb_array_elements(public.hiscores('mining', 50)) r
       where r ->> 'username' = 'RealPlayer')) k),
  array['is_demo', 'rank', 'username', 'xp']::text[],
  'ranked rows carry only allowlisted fields');
select is(
  (select (r ->> 'is_demo')::boolean from jsonb_array_elements(
     public.hiscores('mining', 50)) r
    where r ->> 'username' = 'DemoWillow'),
  true, 'the published demo appears with its label flag');
select is(
  (select (r ->> 'is_demo')::boolean from jsonb_array_elements(
     public.hiscores('mining', 50)) r
    where r ->> 'username' = 'RealPlayer'),
  false, 'real players are not labeled as demos');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.hiscores('mining', 50)) r where r ->> 'username' = 'TestTom'),
  0, 'test-classified players stay invisible publicly');
select is(
  (select r ->> 'username' from jsonb_array_elements(
     public.hiscores('mining', 50)) r limit 1),
  'RealPlayer', 'demos rank under the same rules (no pinning)');
select throws_ok(
  $$ select public._hiscores_public('mining', 50) $$,
  '42501', null, 'the public helper is not callable directly');
select is(
  (select array_agg(k order by k)::text[] from jsonb_object_keys(
     public.public_profile('demowillow')) k),
  array['is_demo', 'state', 'username']::text[],
  'public profile exposes exactly username/is_demo/state');
select is(
  (select array_agg(k order by k)::text[] from jsonb_object_keys(
     public.public_profile('demowillow') -> 'state') k),
  array['xp']::text[],
  'profile state wrapper contains only the xp table');
select is(
  (public.public_profile('demowillow') -> 'state' -> 'xp' ->> 'mining')
    ::bigint,
  5000000::bigint, 'profile xp matches the authoritative game state');
select throws_ok(
  $$ select public.public_profile('testtom') $$,
  '02000', 'no_profile', 'retired test profiles are not publicly findable');
select throws_ok(
  $$ select public.hiscores('farming', 50) $$,
  '22000', 'bad_skill', 'unknown skills are rejected');
reset role;

-- 5. Ordinary updates never clear the demo flag.
update public.players set username_display = 'DemoWillow'
 where username_norm = 'demowillow';
select is(
  (select is_demo from public.players where username_norm = 'demowillow'),
  true, 'demo flag survives ordinary player updates');

select * from finish();
rollback;
