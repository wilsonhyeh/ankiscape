-- server/supabase/tests/0006_account_deletion.test.sql - Deletion cleanup.
-- Run: cd server && supabase test db
-- Covers migration 0010: the BEFORE DELETE trigger on public.players removes
-- the game's checkpoints and the user's moderation audit rows in the same
-- transaction as the Auth deletion, while Auth cascades cover operations,
-- review claims and game_state. A failed deletion rolls the cleanup back.
begin;
select plan(18);

-- ---------------------------------------------------------------------------
-- Fixtures: ordinary player, fixture-registered player, untouched player.
-- ---------------------------------------------------------------------------
insert into public.fixture_registry
  (suite_id, suite_version, username_norm, expected_trace_hash, reserved_email)
values ('account-deletion', 1, 'fixdel', 'trace-hash-fixdel',
        'fixdel@account-deletion.example.invalid');

insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values
  ('50000000-0000-4000-8000-000000000001', '', 'authenticated',
   'deluser@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"deluser","username_display":"DelUser"}'),
  ('50000000-0000-4000-8000-000000000002', '', 'authenticated',
   'fixdel@account-deletion.example.invalid',
   crypt('pw123456', gen_salt('bf')), now(), now(), now(),
   '{"username_norm":"fixdel","username_display":"FixDel"}'),
  ('50000000-0000-4000-8000-000000000003', '', 'authenticated',
   'keepuser@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"keepuser","username_display":"KeepUser"}');

update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000001'
 where username_norm = 'deluser';
update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000002'
 where username_norm = 'fixdel';
update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000003'
 where username_norm = 'keepuser';

insert into public.game_state(game_uuid, user_id, xp, revision)
select p.game_uuid, p.user_id, jsonb_build_object('mining', 1000), 1
  from public.players p
 where p.username_norm in ('deluser', 'fixdel', 'keepuser');

insert into public.game_operations(op_id, game_uuid, user_id, device_id,
                                   device_seq, lamport, kind, payload,
                                   payload_hash)
select gen_random_uuid(), p.game_uuid, p.user_id, 'dev-a', 1, 1,
       'review_award',
       jsonb_build_object('review_key', 'rk-' || p.username_norm),
       'test-hash'
  from public.players p
 where p.username_norm in ('deluser', 'fixdel', 'keepuser');

insert into public.review_claims(game_uuid, review_key, provenance, award_op_id)
select p.game_uuid, 'rk-' || p.username_norm, 'direct', o.op_id
  from public.players p
  join public.game_operations o on o.game_uuid = p.game_uuid
 where p.username_norm in ('deluser', 'fixdel', 'keepuser');

insert into public.game_checkpoints(game_uuid, revision, state)
select p.game_uuid, 1, jsonb_build_object('xp_micro', jsonb_build_object(
         'mining', 1000))
  from public.players p
 where p.username_norm in ('deluser', 'fixdel', 'keepuser');

insert into public.moderation_audit(target_user_id, action, reason)
select p.user_id, 'note', 'pre-delete-fixture'
  from public.players p
 where p.username_norm in ('deluser', 'fixdel', 'keepuser');

-- 1. The cleanup trigger is installed and restricted.
select is(
  (select count(*)::int from pg_trigger
    where tgname = 'ankiscape_account_delete_cleanup'
      and not tgisinternal),
  1, 'the players cleanup trigger is installed');
select ok(
  not has_function_privilege('anon', 'public._account_delete_cleanup()',
                             'EXECUTE'),
  'anon cannot execute the cleanup function');
select ok(
  not has_function_privilege('authenticated',
                             'public._account_delete_cleanup()', 'EXECUTE'),
  'authenticated cannot execute the cleanup function');

-- 2. Auth deletion cascades everything and cleans the two orphan tables.
delete from auth.users
 where id = '50000000-0000-4000-8000-000000000001';
select is(
  (select count(*)::int from public.players p
    where p.username_norm = 'deluser'), 0, 'player row removed');
select is(
  (select count(*)::int from public.game_state s
    where s.game_uuid = 'dddddddd-0000-4000-8000-000000000001'),
  0, 'game state removed by cascade');
select is(
  (select count(*)::int from public.game_operations o
    where o.game_uuid = 'dddddddd-0000-4000-8000-000000000001'),
  0, 'operations removed by cascade');
select is(
  (select count(*)::int from public.review_claims c
    where c.game_uuid = 'dddddddd-0000-4000-8000-000000000001'),
  0, 'review claims removed by cascade');
select is(
  (select count(*)::int from public.game_checkpoints k
    where k.game_uuid = 'dddddddd-0000-4000-8000-000000000001'),
  0, 'checkpoints removed by the cleanup trigger');
select is(
  (select count(*)::int from public.moderation_audit a
    where a.target_user_id = '50000000-0000-4000-8000-000000000001'),
  0, 'moderation audit rows removed by the cleanup trigger');

-- 3. The other player is untouched.
select is(
  (select count(*)::int from public.game_checkpoints k
    where k.game_uuid = 'dddddddd-0000-4000-8000-000000000003'),
  1, 'another game checkpoint is untouched');
select is(
  (select count(*)::int from public.moderation_audit a
    where a.target_user_id = '50000000-0000-4000-8000-000000000003'),
  1, 'another user audit row is untouched');
select is(
  (select count(*)::int from public.game_state s
    where s.game_uuid = 'dddddddd-0000-4000-8000-000000000003'),
  1, 'another game state is untouched');

-- 4. A fixture-registered account cannot be deleted; the whole cleanup rolls
--    back with the failed auth deletion.
select throws_ok(
  $$ delete from auth.users
      where id = '50000000-0000-4000-8000-000000000002' $$,
  '23503', null, 'a registered fixture account cannot be deleted');
select is(
  (select count(*)::int from public.players p
    where p.username_norm = 'fixdel'), 1, 'fixture player row survives');
select is(
  (select count(*)::int from public.game_checkpoints k
    where k.game_uuid = 'dddddddd-0000-4000-8000-000000000002'),
  1, 'fixture checkpoint rolls back with the failed deletion');
select is(
  (select count(*)::int from public.moderation_audit a
    where a.target_user_id = '50000000-0000-4000-8000-000000000002'),
  1, 'fixture audit row rolls back with the failed deletion');

-- 5. Scoring for a deleted game fails closed: no new orphan checkpoint can
--    be written once the owning player row is gone.
select set_config('request.jwt.claims',
  '{"sub":"50000000-0000-4000-8000-000000000001","role":"authenticated"}',
  true);
select throws_ok(
  $$ select public.submit_operations(
       'dddddddd-0000-4000-8000-000000000001', '[]'::jsonb) $$,
  '28000', 'unverified', 'deleted players can no longer write scoring');
reset role;

-- 6. A direct players-row deletion (no Auth cascade) still cleans the
--    orphan tables through the same trigger.
delete from public.players where username_norm = 'keepuser';
select is(
  (select count(*)::int from public.game_checkpoints k
    where k.game_uuid = 'dddddddd-0000-4000-8000-000000000003'),
  0, 'direct player deletion also cleans checkpoints');

select * from finish();
rollback;
