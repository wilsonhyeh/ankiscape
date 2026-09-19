-- server/supabase/tests/0007_account_identity.test.sql - 3.0 account identity.
-- Run: cd server && supabase test db
-- Covers migration 0011: link_game returns the server-owned account game
-- (created/resumed; never game_mismatch/game_claimed), players.visible_on_board
-- + set_board_visibility, self_context's new field, the board_visibility
-- capability, the two public predicates (0009:72 / 0009:96), test-cohort
-- non-interference, the retained ownership guard (S9 negative case), and the
-- cross-account link path (S10).
begin;
select plan(32);

-- ---------------------------------------------------------------------------
-- Fixtures: Alice + Bob (public cohort) and Tester (born test-classified by
-- its suite-owned reservation).
-- ---------------------------------------------------------------------------
insert into public.fixture_registry
  (suite_id, suite_version, username_norm, expected_trace_hash, reserved_email)
values ('account-identity', 1, 'identitytester', 'trace-hash-identitytester',
        'identitytester@account-identity.example.invalid');

insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values
  ('60000000-0000-4000-8000-000000000001', '', 'authenticated',
   'identity-alice@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"identityalice","username_display":"IdentityAlice"}'),
  ('60000000-0000-4000-8000-000000000002', '', 'authenticated',
   'identity-bob@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"identitybob","username_display":"IdentityBob"}'),
  ('60000000-0000-4000-8000-000000000003', '', 'authenticated',
   'identitytester@account-identity.example.invalid',
   crypt('pw123456', gen_salt('bf')), now(), now(), now(),
   '{"username_norm":"identitytester","username_display":"IdentityTester"}')
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- 1. link_game creates the account game and ignores the offered uuid (D5).
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select public.link_game('11111111-1111-1111-1111-111111111111') as alice_link \gset
select (:'alice_link'::jsonb)->>'game_uuid' as alice_game \gset
reset role;
select is((:'alice_link'::jsonb)->>'created', 'true',
          'first link creates the account game');
select is((:'alice_link'::jsonb)->>'resumed', 'false',
          'first link is never a resume (created means not resumed)');
select ok((:'alice_link'::jsonb)->>'game_uuid'
            <> '11111111-1111-1111-1111-111111111111',
          'the offered uuid is ignored; the server owns the game uuid');

-- ---------------------------------------------------------------------------
-- 2. S10: cross-account link. Account B offers account A's offered uuid; the
--    pre-0011 server raised game_claimed/23505 (recorded in the failing run),
--    0011 ignores the offered uuid for identity instead.
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000002","role":"authenticated"}', true);
set local role authenticated;
select public.link_game('11111111-1111-1111-1111-111111111111') as bob_link \gset
reset role;
select is((:'bob_link'::jsonb)->>'created', 'true',
          'a foreign offered uuid never raises game_claimed: B creates a game');
select ok((:'bob_link'::jsonb)->>'game_uuid'
            <> '11111111-1111-1111-1111-111111111111',
          'B receives its own server-owned uuid, not the offered one');
select ok((:'bob_link'::jsonb)->>'game_uuid' <> :'alice_game',
          'the two accounts never share a game');

-- ---------------------------------------------------------------------------
-- 3. Same account, a second offered uuid: returns the bound game, created=false
--    (the old contract raised game_mismatch here).
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select public.link_game('22222222-2222-2222-2222-222222222222') as alice_second \gset
reset role;
select is((:'alice_second'::jsonb)->>'game_uuid', :'alice_game',
          'a second offered uuid returns the account game');
select is((:'alice_second'::jsonb)->>'created', 'false',
          'repeat link reports created=false');
select is((:'alice_second'::jsonb)->>'resumed', 'true',
          'repeat link reports resumed=true');

-- ---------------------------------------------------------------------------
-- 4. S9: the retained ownership guard. An unowned uuid is still refused; the
--    returned uuid passes the same guard.
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select throws_ok(
  $$ select public.submit_operations(
       '99999999-9999-4999-8999-999999999999',
       '[{"op_id":"77777777-0000-4000-8000-000000000001","device_id":"dev-x","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-unowned"}}]'::jsonb) $$,
  '42501', 'game_mismatch',
  'an unowned game uuid is refused by the retained ownership guard');
select lives_ok(
  format('select public.submit_operations(%L, %L::jsonb)',
         :'alice_game',
         '[{"op_id":"77777777-0000-4000-8000-000000000002","device_id":"dev-a","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-identity"}}]'),
  'the returned uuid is accepted by the same guard');
reset role;
select is(
  (select count(*)::int from public.game_operations
    where game_uuid = :'alice_game'),
  1, 'the accepted operation landed under the returned uuid');

-- ---------------------------------------------------------------------------
-- 5. visible_on_board: default, capability, self_context.
-- ---------------------------------------------------------------------------
select is(
  (select visible_on_board from public.players
    where username_norm = 'identityalice'),
  true, 'visible_on_board defaults true');
select is(
  (public.evolved_capabilities()->>'board_visibility')::boolean,
  true, 'capabilities advertise the board-visibility toggle');
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select is((public.self_context()->>'visible_on_board')::boolean, true,
          'self_context reports the current flag');

-- ---------------------------------------------------------------------------
-- 6. Toggle off: the PUBLIC predicates filter; game state is untouched.
-- ---------------------------------------------------------------------------
reset role;
select (select xp::text from public.game_state
         where game_uuid = :'alice_game') as alice_xp \gset
set local role authenticated;
select is(public.set_board_visibility(false)->>'visible_on_board', 'false',
          'set_board_visibility persists the disabled flag');
select is((public.self_context()->>'visible_on_board')::boolean, false,
          'self_context reflects the disabled flag');
reset role;
set local role anon;
select is((select count(*)::int from jsonb_array_elements(
             public.hiscores('mining', 50)) r
           where r->>'username' = 'IdentityAlice'),
          0, 'a hidden real player is filtered from the public board');
select throws_ok(
  $$ select public.public_profile('identityalice') $$,
  '02000', 'no_profile', 'a hidden real profile is not publicly findable');
reset role;
select is(
  (select xp::text from public.game_state where game_uuid = :'alice_game'),
  :'alice_xp', 'toggling never touches game_state.xp');
select is(
  (select count(*)::int from public.game_operations
    where game_uuid = :'alice_game'),
  1, 'toggling never touches the operation log');

-- ---------------------------------------------------------------------------
-- 7. The toggle validates input and is authenticated-only (players is fully
--    revoked, so security definer is load-bearing).
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select throws_ok(
  $$ select public.set_board_visibility(null) $$,
  '22023', 'invalid_visibility', 'a null visibility is rejected');
reset role;
select ok(not has_function_privilege('anon',
          'public.set_board_visibility(boolean)', 'execute'),
          'anon cannot execute set_board_visibility');
select ok(not has_table_privilege('authenticated', 'public.players', 'select'),
          'authenticated has no direct players access (definer required)');

-- ---------------------------------------------------------------------------
-- 8. Cohort non-interference: the 0006 test RPCs do not gain the flag.
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000003","role":"authenticated"}', true);
set local role authenticated;
select public.link_game('33333333-3333-3333-3333-333333333333')->>'game_uuid'
       as tester_game \gset
select is(public.set_board_visibility(false)->>'visible_on_board', 'false',
          'a test-cohort player can turn off board visibility');
select is((select count(*)::int from jsonb_array_elements(
             public.test_hiscores('mining', 50)) r
           where r->>'username' = 'IdentityTester'),
          1, 'the cohort leaderboard ignores board visibility');
select is(public.test_public_profile('identitytester')->>'username',
          'IdentityTester', 'the cohort profile lookup ignores board visibility');
reset role;
set local role anon;
select is((select count(*)::int from jsonb_array_elements(
             public.hiscores('mining', 50)) r
           where r->>'username' = 'IdentityTester'),
          0, 'test-cohort rows stay off the public board');
select throws_ok(
  $$ select public.public_profile('identitytester') $$,
  '02000', 'no_profile', 'test-cohort profiles stay off the public lookup');
reset role;

-- ---------------------------------------------------------------------------
-- 9. Restore: the toggle is reversible and the public paths come back.
-- ---------------------------------------------------------------------------
select set_config('request.jwt.claims',
  '{"sub":"60000000-0000-4000-8000-000000000001","role":"authenticated"}', true);
set local role authenticated;
select is(public.set_board_visibility(true)->>'visible_on_board', 'true',
          'the toggle restores public visibility');
reset role;
set local role anon;
select is((select count(*)::int from jsonb_array_elements(
             public.hiscores('mining', 50)) r
           where r->>'username' = 'IdentityAlice'),
          1, 'the restored player reappears on the public board');
select is(public.public_profile('identityalice')->>'username', 'IdentityAlice',
          'the restored profile is publicly findable again');
reset role;

select * from finish();
rollback;
