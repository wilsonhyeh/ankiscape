-- server/supabase/tests/0002_ownership.test.sql - link/submit/fetch/hiscores.
-- Run: cd server && supabase test db
begin;
select plan(13);

-- Verified Alice links a fresh game; repeat resumes; other game conflicts.
insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '', 'authenticated',
        'a@example.com', crypt('pw123456', gen_salt('bf')),
        now(), now(), now(), '{"username_norm":"alice","username_display":"Alice"}')
on conflict (id) do nothing;

select set_config('request.jwt.claims',
  '{"sub":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","role":"authenticated"}', true);
set local role authenticated;
select is(
  (select public.link_game('11111111-1111-1111-1111-111111111111')->>'resumed'),
  'false', 'first link binds the game');
select is(
  (select public.link_game('11111111-1111-1111-1111-111111111111')->>'resumed'),
  'true', 'repeat link resumes the same binding');
select throws_ok(
  $$ select public.link_game('22222222-2222-2222-2222-222222222222') $$,
  '23505', 'game_mismatch', 'second game mismatches');
reset role;

-- Submit one op; exact retry returns the same acceptance; changed payload
-- with a reused id returns a conflict, not a second row.
select set_config('request.jwt.claims',
  '{"sub":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","role":"authenticated"}', true);
set local role authenticated;
select is(
  (select jsonb_array_length(
     public.submit_operations('11111111-1111-1111-1111-111111111111',
       '[{"op_id":"bbbbbbbb-2222-4444-8888-eeeeeeeeeeee","device_id":"dev-a","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-1"}}]'::jsonb)
     -> 'accepted')),
  1, 'first submit accepted');
select is(
  (select jsonb_array_length(
     public.submit_operations('11111111-1111-1111-1111-111111111111',
       '[{"op_id":"bbbbbbbb-2222-4444-8888-eeeeeeeeeeee","device_id":"dev-a","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-1"}}]'::jsonb)
     -> 'accepted')),
  1, 'exact retry returns the same acceptance');
select is(
  (select jsonb_array_length(
     public.submit_operations('11111111-1111-1111-1111-111111111111',
       '[{"op_id":"bbbbbbbb-2222-4444-8888-eeeeeeeeeeee","device_id":"dev-a","device_seq":1,"lamport":1,"kind":"review_award","payload":{"review_key":"rk-CHANGED"}}]'::jsonb)
     -> 'conflicts')),
  1, 'reused id with changed content conflicts');
reset role;
-- Direct row check runs as the test runner (authenticated has zero grants).
select is(
  (select count(*)::int from public.game_operations
   where op_id = 'bbbbbbbb-2222-4444-8888-eeeeeeeeeeee'),
  1, 'no duplicate row stored');

-- Batch size guard fires before any write (201 ops rejected).
set local role authenticated;
select throws_ok(
  $$ select public.submit_operations('11111111-1111-1111-1111-111111111111',
       (select jsonb_agg(jsonb_build_object('op_id', md5(g::text)::uuid, 'device_id', 'dev-a',
                                            'device_seq', g, 'lamport', g,
                                            'kind', 'review_award',
                                            'payload', jsonb_build_object('review_key', g::text)))
        from generate_series(1, 201) g)) $$,
  '22000', 'ops_batch_size', 'batches over 200 rejected');
reset role;

-- Unverified Carol is blocked from linking (verified gate, not just login).
insert into auth.users(id, aud, role, email, encrypted_password,
                       created_at, updated_at, raw_user_meta_data)
values ('cccccccc-cccc-cccc-cccc-cccccccccccc', '', 'authenticated',
        'c@example.com', crypt('pw123456', gen_salt('bf')),
        now(), now(), '{"username_norm":"carol","username_display":"Carol"}')
on conflict (id) do nothing;
select set_config('request.jwt.claims',
  '{"sub":"cccccccc-cccc-cccc-cccc-cccccccccccc","role":"authenticated"}', true);
set local role authenticated;
select throws_ok(
  $$ select public.link_game('33333333-3333-3333-3333-333333333333') $$,
  '28000', 'unverified', 'unverified user cannot link');
reset role;

-- Duplicate username signup aborts with no orphaned profile.
select throws_ok(
  $$ insert into auth.users(id, aud, role, email, encrypted_password,
                            created_at, updated_at, raw_user_meta_data)
     values ('dddddddd-dddd-dddd-dddd-dddddddddddd', '', 'authenticated',
             'd@example.com', crypt('pw123456', gen_salt('bf')),
             now(), now(), '{"username_norm":"alice","username_display":"Alice2"}') $$,
  '23505', 'username_taken', 'duplicate username signup aborted');
select is(
  (select count(*)::int from auth.users
   where id = 'dddddddd-dddd-dddd-dddd-dddddddddddd'),
  0, 'no orphaned auth user left behind');

-- Hiscores allowlist + fetch paging shape on the linked game.
select set_config('request.jwt.claims',
  '{"sub":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","role":"authenticated"}', true);
set local role authenticated;
select ok(jsonb_typeof(public.hiscores('mining', 10)) = 'array',
          'hiscores returns an array');
select ok((public.fetch_operations('11111111-1111-1111-1111-111111111111',
                                   0, 200) ->> 'revision') is not null,
          'fetch returns a revision');
reset role;

select * from finish();
rollback;
