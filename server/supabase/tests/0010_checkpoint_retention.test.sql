-- server/supabase/tests/0010_checkpoint_retention.test.sql - game_checkpoints
-- stays small (migrations 0015 + 0017).
-- Run: cd server && supabase test db
-- submit_operations used to append a full-state checkpoint on every accepted
-- submit and never remove one; on production that table reached 406 MB. 0015
-- kept one row per game; 0017 stops writing it (nothing reads it) and keeps
-- the scoring state in game_fold instead. Submits must write no checkpoint,
-- keep a current fold, and not touch another game's checkpoint rows.
begin;
select plan(5);

insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
  email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at,
  updated_at, confirmation_token, recovery_token, email_change_token_new,
  email_change, email_change_token_current, phone_change, phone_change_token,
  reauthentication_token)
values ('99999999-9999-4999-8999-999999999999',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgckpt@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgckpt","username_display":"pgckpt"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', '')
on conflict (id) do nothing;

update public.players set game_uuid = 'aaaaaaaa-9999-4999-8999-999999999999'
 where user_id = '99999999-9999-4999-8999-999999999999';
insert into public.game_state(game_uuid, user_id)
values ('aaaaaaaa-9999-4999-8999-999999999999',
        '99999999-9999-4999-8999-999999999999')
on conflict (game_uuid) do nothing;

-- Another game's checkpoint history must survive this game's submits.
insert into public.game_checkpoints(game_uuid, revision, state)
values ('bbbbbbbb-9999-4999-8999-999999999999', 1, '{}'::jsonb),
       ('bbbbbbbb-9999-4999-8999-999999999999', 2, '{}'::jsonb);

select set_config('request.jwt.claims',
  '{"sub":"99999999-9999-4999-8999-999999999999","role":"authenticated"}', true);
set local role authenticated;
select public.submit_operations('aaaaaaaa-9999-4999-8999-999999999999',
  jsonb_build_array(jsonb_build_object(
    'op_id', md5('pgckpt-' || i)::uuid, 'device_id', 'dev-a',
    'device_seq', i, 'lamport', i, 'kind', 'review_award',
    'payload', jsonb_build_object('review_key', 'rk-' || i, 'review_ts', i,
      'provenance', 'direct', 'reward_policy', 2,
      'skill', 'mining', 'resource', 'Copper ore'))))
  from generate_series(1, 3) as i;
reset role;

select is(
  (select revision::int from public.game_state
    where game_uuid = 'aaaaaaaa-9999-4999-8999-999999999999'),
  3, 'three accepted submits advanced the revision three times');
select is(
  (select count(*)::int from public.game_checkpoints
    where game_uuid = 'aaaaaaaa-9999-4999-8999-999999999999'),
  0, 'submits no longer write game_checkpoints');
select is(
  (select folded_ops::int from public.game_fold
    where game_uuid = 'aaaaaaaa-9999-4999-8999-999999999999' and not stale),
  3, 'the fold covers all three operations and is current');
select is(
  (select xp from public.game_state
    where game_uuid = 'aaaaaaaa-9999-4999-8999-999999999999'),
  public.evolved_replay('aaaaaaaa-9999-4999-8999-999999999999')->'xp_micro',
  'game_state holds the full-replay XP');
select is(
  (select count(*)::int from public.game_checkpoints
    where game_uuid = 'bbbbbbbb-9999-4999-8999-999999999999'),
  2, 'another game''s checkpoints are untouched');

select * from finish();
rollback;
