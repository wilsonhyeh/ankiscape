-- server/supabase/tests/0003_scoring.test.sql - Authoritative scoring +
-- authorization surface for the 3.0 scoring migration.
-- Run: cd server && supabase test db
begin;
select plan(14);

-- Rules snapshot / capabilities surface.
select is((select rules->>'rules_version' from public.rules_versions
           where version = 1), '1', 'rules v1 stored');
select is((public.evolved_capabilities()->>'protocol_version')::int, 2,
          'capabilities protocol 2');
select is((public.evolved_capabilities()->>'authoritative_scoring')::boolean,
          true, 'authoritative scoring advertised');

-- Synthetic user + game (direct SQL; the HTTP header gate only applies to
-- PostgREST requests and this is the documented legacy-import path).
insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
  email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at,
  updated_at, confirmation_token, recovery_token, email_change_token_new,
  email_change, email_change_token_current, phone_change, phone_change_token,
  reauthentication_token)
values ('33333333-3333-3333-3333-333333333333',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgscore@example.com', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgscore","username_display":"pgscore"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', '')
on conflict (id) do nothing;

update public.players set game_uuid = '44444444-4444-4444-4444-444444444444'
 where user_id = '33333333-3333-3333-3333-333333333333';
insert into public.game_state(game_uuid, user_id)
values ('44444444-4444-4444-4444-444444444444',
        '33333333-3333-3333-3333-333333333333')
on conflict (game_uuid) do nothing;

-- Operations: policy-2 missing materials -> zero; policy-1 -> practice;
-- catch-up award suppressed by a Classic-mode skip claim.
insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash)
values
 ('aaaaaaaa-0000-0000-0000-000000000001',
  '44444444-4444-4444-4444-444444444444',
  '33333333-3333-3333-3333-333333333333', 'dev-a', 1, 1, 'review_award',
  '{"review_key":"rk-smith-2","review_ts":1000,"rating":3,"review_kind":"review","provenance":"direct","reward_policy":2,"skill":"smithing","resource":"Bronze bar"}'::jsonb, ''),
 ('aaaaaaaa-0000-0000-0000-000000000002',
  '44444444-4444-4444-4444-444444444444',
  '33333333-3333-3333-3333-333333333333', 'dev-a', 2, 2, 'review_award',
  '{"review_key":"rk-smith-1","review_ts":1001,"rating":3,"review_kind":"review","provenance":"direct","reward_policy":1,"skill":"smithing","resource":"Bronze bar"}'::jsonb, ''),
 ('aaaaaaaa-0000-0000-0000-000000000003',
  '44444444-4444-4444-4444-444444444444',
  '33333333-3333-3333-3333-333333333333', 'dev-a', 3, 3, 'review_skip',
  '{"review_key":"rk-skip","reason":"classic_mode","provenance":"skip"}'::jsonb, ''),
 ('aaaaaaaa-0000-0000-0000-000000000004',
  '44444444-4444-4444-4444-444444444444',
  '33333333-3333-3333-3333-333333333333', 'dev-a', 4, 4, 'review_award',
  '{"review_key":"rk-skip","review_ts":1002,"rating":3,"review_kind":"review","provenance":"catchup","reward_policy":2}'::jsonb, '');

select is(
  public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'review_outcomes'->'rk-smith-2'->>'outcome',
  'paused_materials', 'policy-2 missing materials pauses with zero');
select is(
  (public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'review_outcomes'->'rk-smith-2'->>'rewarded')::boolean,
  false, 'paused review is not a reward');
select is(
  public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'review_outcomes'->'rk-smith-1'->>'outcome',
  'practice', 'policy-1 keeps historical practice XP');
select is(
  public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'xp_micro'->>'smithing',
  '1000000', 'only the policy-1 practice XP landed');
select is(
  public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'review_outcomes'->>'rk-skip',
  null, 'skip claim suppresses the catch-up award');
select ok(
  (public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'diagnostics')::text like '%claim_skipped:rk-skip%',
  'skip suppression is diagnosed');
select is(
  public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'inventory',
  '{}'::jsonb, 'no items produced while paused');

-- Unsupported policy is rejected explicitly (isolated game so the other
-- fixtures keep replaying).
insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash)
values ('aaaaaaaa-0000-0000-0000-000000000005',
        '55555555-5555-5555-5555-555555555555',
        '33333333-3333-3333-3333-333333333333', 'dev-a', 5, 5, 'review_award',
        '{"review_key":"rk-bad","review_ts":1003,"rating":3,"review_kind":"review","provenance":"direct","reward_policy":3,"skill":"mining","resource":"Rune essence"}'::jsonb, '');
select throws_ok(
  $$ select public.evolved_replay('55555555-5555-5555-5555-555555555555') $$,
  '22000',
  'unsupported_policy:3 op aaaaaaaa-0000-0000-0000-000000000005',
  'unsupported reward policy raises');

-- Direct SQL submission (legacy import path) accepts and replays atomically.
select set_config('request.jwt.claims',
  '{"sub":"33333333-3333-3333-3333-333333333333","role":"authenticated"}', true);
set local role authenticated;
select lives_ok(
  $$ select public.submit_operations(
       '44444444-4444-4444-4444-444444444444',
       '[{"op_id":"aaaaaaaa-0000-0000-0000-000000000006",
          "device_id":"dev-a","device_seq":6,"lamport":6,
          "kind":"review_retract",
          "payload":{"target_review_key":"rk-smith-1","reason":"test"}}]'::jsonb) $$,
  'submit_operations accepts a retraction without headers');
reset role;
select is(
  (public.evolved_replay('44444444-4444-4444-4444-444444444444')
    ->'xp_micro'->>'smithing')::bigint,
  0::bigint, 'retraction reverses the practice XP');

-- Authorization: anon cannot call the replay directly.
set local role anon;
select throws_ok(
  $$ select public.evolved_replay('44444444-4444-4444-4444-444444444444') $$,
  '42501', 'permission denied for function evolved_replay',
  'anon cannot replay arbitrary games');
reset role;

select * from finish();
rollback;
