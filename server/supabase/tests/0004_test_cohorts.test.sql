-- server/supabase/tests/0004_test_cohorts.test.sql - Cohort isolation.
-- Run: cd server && supabase test db
-- Covers: classification birth/forgery, public-path exclusion, test-path
-- authorization, ties/limits/banned, safe profile allowlist, grants and
-- direct-write denial.
begin;
select plan(33);

-- ---------------------------------------------------------------------------
-- Fixtures: reservations exist BEFORE any account (born classified).
-- ---------------------------------------------------------------------------
insert into public.fixture_registry
  (suite_id, suite_version, username_norm, expected_trace_hash, reserved_email)
values
  ('hosted-v1', 1, 'testtom', 'trace-hash-tom',
   'testtom@example.invalid'),
  ('hosted-v1', 1, 'testtina', 'trace-hash-tina',
   'testtina@example.invalid'),
  ('hosted-v1', 1, 'unclaimed', 'trace-hash-unclaimed',
   'unclaimed@hosted-v1.example.invalid'),
  ('hosted-v1', 1, 'reservedok', 'trace-hash-reservedok',
   'reservedok@hosted-v1.example.invalid');

insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values
  ('10000000-0000-4000-8000-000000000001', '', 'authenticated',
   'testtom@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"testtom","username_display":"TestTom","is_test":false}'),
  ('10000000-0000-4000-8000-000000000002', '', 'authenticated',
   'testtina@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"testtina","username_display":"TestTina"}'),
  ('20000000-0000-4000-8000-000000000001', '', 'authenticated',
   'alice@example.com', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"alice","username_display":"Alice","is_test":true}'),
  ('20000000-0000-4000-8000-000000000002', '', 'authenticated',
   'bob@example.com', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"bob","username_display":"Bob"}'),
  ('20000000-0000-4000-8000-000000000003', '', 'authenticated',
   'ban@example.com', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"banneduser","username_display":"BannedUser"}')
on conflict (id) do nothing;

-- Reservation names are held: an ordinary signup cannot claim one.
select throws_ok(
  $$ insert into auth.users(id, aud, role, email, encrypted_password,
                            email_confirmed_at, created_at, updated_at,
                            raw_user_meta_data)
     values ('30000000-0000-4000-8000-000000000001', '', 'authenticated',
             'thief@example.com', crypt('pw123456', gen_salt('bf')),
             now(), now(), now(),
             '{"username_norm":"unclaimed","username_display":"Thief"}') $$,
  '23505', 'username_taken', 'reserved fixture names cannot be taken over');

-- A reservation is claimable only by its suite-owned address.
select throws_ok(
  $$ insert into auth.users(id, aud, role, email, encrypted_password,
                            email_confirmed_at, created_at, updated_at,
                            raw_user_meta_data)
     values ('30000000-0000-4000-8000-000000000002', '', 'authenticated',
             'wrong@example.com', crypt('pw123456', gen_salt('bf')),
             now(), now(), now(),
             '{"username_norm":"reservedok","username_display":"ReservedOk"}') $$,
  '23505', 'username_taken',
  'reserved names reject non-suite email addresses');
select is(
  (select count(*)::int from public.players
    where username_norm = 'reservedok'),
  0, 'rejected reservation left no player row');
insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values ('30000000-0000-4000-8000-000000000003', '', 'authenticated',
        'reservedok@hosted-v1.example.invalid',
        crypt('pw123456', gen_salt('bf')), now(), now(), now(),
        '{"username_norm":"reservedok","username_display":"ReservedOk"}');
select is(
  (select is_test from public.players where username_norm = 'reservedok'),
  true, 'the suite-owned address claims its reservation as test-classified');
select is(
  (select count(*)::int from public.players
    where username_norm in ('testtom','testtina') and is_test),
  2, 'reserved fixture usernames are born test-classified');
select is(
  (select is_test from public.players where username_norm = 'alice'),
  false, 'client metadata cannot forge test identity');
select is(
  (select is_test from public.players where username_norm = 'bob'),
  false, 'ordinary users stay public-cohort');

-- ---------------------------------------------------------------------------
-- Game state per cohort (Tom/Tina test; Alice/Bob/Banned public).
-- ---------------------------------------------------------------------------
update public.players set game_uuid = 'aaaaaaaa-0000-4000-8000-000000000001'
 where username_norm = 'testtom';
update public.players set game_uuid = 'aaaaaaaa-0000-4000-8000-000000000002'
 where username_norm = 'testtina';
update public.players set game_uuid = 'bbbbbbbb-0000-4000-8000-000000000001'
 where username_norm = 'alice';
update public.players set game_uuid = 'bbbbbbbb-0000-4000-8000-000000000002'
 where username_norm = 'bob';
update public.players set game_uuid = 'bbbbbbbb-0000-4000-8000-000000000003'
 where username_norm = 'banneduser';
update public.players set status = 'banned' where username_norm = 'banneduser';
update public.players set is_test = true where username_norm = 'testtina';

insert into public.game_state(game_uuid, user_id, xp, revision)
select p.game_uuid, p.user_id,
       jsonb_build_object(
         'mining', case p.username_norm
                     when 'testtom' then 9000000
                     when 'testtina' then 9000000
                     when 'alice' then 12000000
                     when 'bob' then 12000000
                     else 99000000 end,
         'woodcutting', 3000000),
       1
  from public.players p
 where p.game_uuid is not null;

-- ---------------------------------------------------------------------------
-- Public paths exclude the test cohort before rank/sort/limit.
-- ---------------------------------------------------------------------------
set local role anon;
select is(
  (select count(*)::int from jsonb_array_elements(
     public.hiscores('mining', 50)) r
    where r->>'username' in ('TestTom','TestTina')),
  0, 'public hiscores never include test players');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.hiscores('mining', 50)) r
    where r->>'username' = 'BannedUser'),
  0, 'public hiscores never include banned players');
select is(
  (public.hiscores('mining', 50) -> 0 ->> 'username'),
  'Alice', 'public rank order and limit unaffected by hidden test rows');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.hiscores('mining', 50)) r where r->>'username' = 'Bob'),
  1, 'public tie-mate still listed');
select is(
  (select (r->>'rank')::int from jsonb_array_elements(
     public.hiscores('mining', 50)) r where r->>'username' = 'Bob'),
  1, 'public ties share rank 1');
select throws_ok(
  $$ select public.public_profile('testtom') $$,
  '02000', 'no_profile', 'public lookup denies test profiles');
select throws_ok(
  $$ select public.public_profile('nobody_here') $$,
  '02000', 'no_profile', 'unknown profile is the same denial');
select ok(
  (select public.public_profile('alice') ->> 'username') = 'Alice',
  'public lookup serves public players');
reset role;

-- ---------------------------------------------------------------------------
-- Test cohort paths require the caller's own row to be test.
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"20000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select throws_ok(
  $$ select public.test_hiscores('mining', 50) $$,
  '02000', 'no_profile', 'public caller cannot use test leaderboard');
select throws_ok(
  $$ select public.test_public_profile('testtom') $$,
  '02000', 'no_profile', 'public caller cannot use test profile lookup');
reset role;

select set_config('request.jwt.claims',
  '{"sub":"10000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select is(
  (select public.self_context() ->> 'is_test')::boolean,
  true, 'self_context reports test cohort');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.test_hiscores('mining', 50)) r
    where r->>'username' in ('TestTom','TestTina')),
  2, 'test leaderboard includes both test players');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.test_hiscores('mining', 50)) r
    where r->>'username' in ('Alice','Bob')),
  0, 'test leaderboard excludes public players');
select is(
  (select count(*)::int from jsonb_array_elements(
     public.test_hiscores('mining', 50)) r
    where (r->>'rank')::int = 1),
  2, 'test ties share rank 1');
select throws_ok(
  $$ select public.test_public_profile('alice') $$,
  '02000', 'no_profile', 'non-test target is indistinguishable');
select ok(
  (select public.test_public_profile('testtom') -> 'skills' -> 'mining'
     ->> 'xp')::bigint = 9000000,
  'test profile exposes allowlisted xp');
select ok(
  not (select public.test_public_profile('testtom') ? 'state'),
  'test profile never exposes full private state');
select throws_ok(
  $$ select public.submit_operations(
       'aaaaaaaa-0000-4000-8000-000000000002',
       '[{"op_id":"99999999-0000-4000-8000-000000000001","device_id":"dev-x","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-x"}}]'::jsonb) $$,
  '42501', 'game_mismatch', 'test users cannot mutate another player''s game');
select throws_like(
  $$ update public.players set is_test = true
      where username_norm = 'bob' $$,
  '%permission denied%',
  'authenticated cannot flip cohort flags');
reset role;

-- ---------------------------------------------------------------------------
-- The registry itself is private.
-- ---------------------------------------------------------------------------
set local role anon;
select throws_like(
  $$ select count(*) from public.fixture_registry $$,
  '%permission denied%', 'fixture registry is not readable');
reset role;
set local role authenticated;
select throws_like(
  $$ select count(*) from public.fixture_registry $$,
  '%permission denied%', 'authenticated cannot read the registry');
reset role;

-- Grants: test RPCs are authenticated-only; public RPCs stay anon-usable.
select ok(not has_function_privilege('anon',
          'public.test_hiscores(text,int)', 'execute'),
          'anon cannot execute test_hiscores');
select ok(has_function_privilege('authenticated',
          'public.test_hiscores(text,int)', 'execute'),
          'authenticated can execute test_hiscores');
select ok(not has_function_privilege('anon',
          'public.self_context()', 'execute'),
          'anon cannot read self_context');
set local role anon;
select ok(jsonb_array_length(public.hiscores('mining', 9999)) <= 100,
          'hiscores limit clamps to 100');
select ok(jsonb_array_length(public.hiscores('mining', 0)) >= 1,
          'hiscores limit floors at 1');
reset role;

-- Banned fixture rows are excluded from test paths too.
-- Clear the request claims first: privileged updates run as the test runner
-- with auth.uid() null (the guard is exactly what blocks authenticated flags).
select set_config('request.jwt.claims', '', true);
update public.players set is_test = true where username_norm = 'banneduser';
update public.players set status = 'banned' where username_norm = 'banneduser';
select set_config('request.jwt.claims',
  '{"sub":"10000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select is(
  (select count(*)::int from jsonb_array_elements(
     public.test_hiscores('mining', 50)) r
    where r->>'username' = 'BannedUser'),
  0, 'banned test players are excluded');
reset role;

select * from finish();
rollback;
