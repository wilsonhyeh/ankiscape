-- server/supabase/tests/0011_incremental_fold.test.sql - incremental scoring
-- (migration 0017).
-- Run: cd server && supabase test db
-- submit_operations folds each upload onto game_fold. Every path must leave
-- game_state equal to evolved_replay: the fast path (direct and catch-up
-- awards, skips, undo/redo with rewind), the synchronous rebuild (duplicate
-- claim, late insertion, preset), and the stale/background path. A 200-op
-- upload on a 50,000-op game must finish under a 1 s statement_timeout.
-- Which path ran is read from game_fold.rebuilt_at: the test sets it to a
-- marker before an upload; only a rebuild overwrites it.
begin;
select plan(18);

insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
  email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at,
  updated_at, confirmation_token, recovery_token, email_change_token_new,
  email_change, email_change_token_current, phone_change, phone_change_token,
  reauthentication_token)
values ('a1111111-1111-4111-8111-111111111111',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgfold@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgfold","username_display":"pgfold"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', ''),
       ('a2222222-2222-4222-8222-222222222222',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgfoldbig@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgfoldbig","username_display":"pgfoldbig"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', '')
on conflict (id) do nothing;

update public.players set game_uuid = 'b1111111-1111-4111-8111-111111111111'
 where user_id = 'a1111111-1111-4111-8111-111111111111';
update public.players set game_uuid = 'b2222222-2222-4222-8222-222222222222'
 where user_id = 'a2222222-2222-4222-8222-222222222222';
insert into public.game_state(game_uuid, user_id)
values ('b1111111-1111-4111-8111-111111111111', 'a1111111-1111-4111-8111-111111111111'),
       ('b2222222-2222-4222-8222-222222222222', 'a2222222-2222-4222-8222-222222222222')
on conflict (game_uuid) do nothing;

-- Helpers (dropped with the transaction).
create function pg_temp.op(i int, kind text, payload jsonb, dev text default 'dev-a',
                           lam int default null)
returns jsonb language sql as $$
  select jsonb_build_object('op_id', md5('pgfold-' || dev || '-' || i)::uuid,
    'device_id', dev, 'device_seq', i, 'lamport', coalesce(lam, i),
    'kind', kind, 'payload', payload)
$$;
create function pg_temp.direct(i int, skill text, resource text)
returns jsonb language sql as $$
  select pg_temp.op(i, 'review_award', jsonb_build_object('review_key', 'rk-' || i,
    'review_ts', 1000 + i, 'provenance', 'direct', 'reward_policy', 2,
    'skill', skill, 'resource', resource))
$$;
create function pg_temp.catchup(i int, ts int)
returns jsonb language sql as $$
  select pg_temp.op(i, 'review_award', jsonb_build_object('review_key', 'rk-' || i,
    'review_ts', ts, 'provenance', 'catchup', 'reward_policy', 2))
$$;
create function pg_temp.submit(ops jsonb) returns jsonb language plpgsql as $$
declare r jsonb;
begin
  perform set_config('request.jwt.claims',
    '{"sub":"a1111111-1111-4111-8111-111111111111","role":"authenticated"}', true);
  set local role authenticated;
  r := public.submit_operations('b1111111-1111-4111-8111-111111111111', ops);
  reset role;
  return r;
end $$;
create function pg_temp.mark() returns void language sql as $$
  update public.game_fold set rebuilt_at = '2000-01-01'
   where game_uuid = 'b1111111-1111-4111-8111-111111111111'
$$;
create function pg_temp.rebuilt() returns boolean language sql as $$
  select rebuilt_at <> '2000-01-01' from public.game_fold
   where game_uuid = 'b1111111-1111-4111-8111-111111111111'
$$;
create function pg_temp.matches() returns boolean language sql as $$
  select s.xp = r->'xp_micro' and s.inventory = r->'inventory'
     and s.counters = r->'counters' and s.achievements = r->'achievements'
     and s.checkpoint->'levels' = r->'levels'
     and s.checkpoint->'diagnostics' = (select coalesce(jsonb_agg(d order by d), '[]'::jsonb)
                                         from jsonb_array_elements_text(r->'diagnostics') d)
    from public.game_state s,
         (select public.evolved_replay('b1111111-1111-4111-8111-111111111111') as r) x
   where s.game_uuid = 'b1111111-1111-4111-8111-111111111111'
$$;

-- 1. First upload: no fold yet, so a synchronous rebuild creates it.
select pg_temp.submit(jsonb_build_array(
  pg_temp.direct(1, 'mining', 'Copper ore'), pg_temp.direct(2, 'mining', 'Tin ore')));
select ok(pg_temp.matches(), 'first upload: game_state equals the full replay');

-- 2. Fast path: direct awards incl. a smithing recipe that consumes ores.
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(
  pg_temp.direct(3, 'smithing', 'Bronze bar'), pg_temp.direct(4, 'woodcutting', 'Tree')));
select ok(not pg_temp.rebuilt() and pg_temp.matches(),
          'direct awards take the fast path and match the full replay');

-- 3. Fast path: undo a recent award (rewind + re-fold), then redo it.
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(5, 'review_retract', '{"target_review_key":"rk-3","reason":"anki_undo"}')));
select ok(not pg_temp.rebuilt() and pg_temp.matches(), 'undo takes the fast path (rewind)');
select is((select status from public.game_review_keys
            where game_uuid = 'b1111111-1111-4111-8111-111111111111' and review_key = 'rk-3'),
          'retracted', 'the undone key is marked retracted');
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(6, 'review_restore', '{"target_review_key":"rk-3","reason":"anki_redo"}')));
select ok(not pg_temp.rebuilt() and pg_temp.matches(), 'redo takes the fast path (rewind)');

-- 4. Fast path: a skip claim for a new key, and catch-up awards.
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(7, 'review_skip', '{"review_key":"rk-skip","reason":"classic_mode","provenance":"skip"}'),
  pg_temp.catchup(8, 50), pg_temp.catchup(9, 5000)));
select ok(not pg_temp.rebuilt() and pg_temp.matches(), 'skips and catch-up awards take the fast path');

-- 5. Fallbacks that rebuild synchronously (small game).
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(10, 'catchup_preset', '{"skill":"fishing","effective_ts":100}')));
select ok(pg_temp.rebuilt() and pg_temp.matches(), 'a preset rebuilds (it can re-time past catch-ups)');
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(pg_temp.catchup(11, 4000)));
select ok(not pg_temp.rebuilt() and pg_temp.matches(), 'a catch-up after a preset uses the preset on the fast path');
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(pg_temp.op(12, 'review_award',
  '{"review_key":"rk-1","review_ts":1,"provenance":"direct","reward_policy":2,"skill":"mining","resource":"Clay"}')));
select ok(pg_temp.rebuilt() and pg_temp.matches(), 'a second claim for a known key rebuilds');
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(14, 'review_retract', '{"target_review_key":"rk-1","reason":"anki_undo"}')));
select ok(pg_temp.rebuilt() and pg_temp.matches(),
          'undo of a key with a duplicate claim rebuilds (its diagnostics change)');
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(pg_temp.op(1, 'review_award',
  '{"review_key":"rk-late","review_ts":2,"provenance":"direct","reward_policy":2,"skill":"mining","resource":"Clay"}',
  'dev-b', 2)));
select ok(pg_temp.rebuilt() and pg_temp.matches(), 'a late canonical insertion rebuilds');

-- 6. Background path: with the synchronous limit at 0 a fallback marks the
--    fold stale and leaves game_state alone; the job then rebuilds it.
select set_config('ankiscape.sync_rebuild_max_ops', '0', true);
select pg_temp.mark();
select pg_temp.submit(jsonb_build_array(
  pg_temp.op(15, 'catchup_preset', '{"skill":"woodcutting","effective_ts":3000}')));
select ok((select stale from public.game_fold
            where game_uuid = 'b1111111-1111-4111-8111-111111111111'),
          'over the limit, a fallback marks the fold stale');
select is(public._evolved_fold_rebuild_stale(10), 1, 'the background job rebuilds one stale game');
select ok(pg_temp.matches() and not (select stale from public.game_fold
            where game_uuid = 'b1111111-1111-4111-8111-111111111111'),
          'after the job, game_state equals the full replay');
select set_config('ankiscape.sync_rebuild_max_ops', '', true);

-- 7. Scale: 50,000 operations, then a 200-op upload (incl. an undo of its
--    own first award) as authenticated under a 1 s statement_timeout.
insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash)
select md5('pgfoldbig-' || i)::uuid, 'b2222222-2222-4222-8222-222222222222',
       'a2222222-2222-4222-8222-222222222222', 'dev-a', i, i, 'review_award',
       jsonb_build_object('review_key', 'rk-' || i, 'review_ts', 1000 + i,
         'provenance', 'direct', 'reward_policy', 2,
         'skill', (array['mining','woodcutting'])[1 + i % 2],
         'resource', (array['Copper ore','Tree'])[1 + i % 2]), ''
  from generate_series(1, 50000) as i;
select public._evolved_fold_rebuild('b2222222-2222-4222-8222-222222222222', false);
select is((select folded_ops::int from public.game_fold
            where game_uuid = 'b2222222-2222-4222-8222-222222222222'),
          50000, 'the 50,000-op fold is built');
select is((select count(*)::int from public.game_fold_snapshots
            where game_uuid = 'b2222222-2222-4222-8222-222222222222'),
          20, 'only the newest 20 snapshots are kept');

select set_config('request.jwt.claims',
  '{"sub":"a2222222-2222-4222-8222-222222222222","role":"authenticated"}', true);
set local role authenticated;
set local statement_timeout = '1s';
select lives_ok(
  $$ select public.submit_operations('b2222222-2222-4222-8222-222222222222',
       (select jsonb_agg(o order by n) from (
          select i as n, jsonb_build_object('op_id', md5('pgfoldbig-' || i)::uuid,
            'device_id', 'dev-a', 'device_seq', i, 'lamport', i, 'kind', 'review_award',
            'payload', jsonb_build_object('review_key', 'rk-' || i, 'review_ts', 1000 + i,
              'provenance', case when i % 5 = 0 then 'catchup' else 'direct' end,
              'reward_policy', 2, 'skill', 'mining', 'resource', 'Clay')) as o
            from generate_series(50001, 50199) as i
          union all
          select 50200, jsonb_build_object('op_id', md5('pgfoldbig-50200')::uuid,
            'device_id', 'dev-a', 'device_seq', 50200, 'lamport', 50200,
            'kind', 'review_retract',
            'payload', '{"target_review_key":"rk-50001","reason":"anki_undo"}'::jsonb)) b)) $$,
  'a 200-op upload with an undo on a 50,000-op game finishes under 1 s');
reset statement_timeout;
reset role;
select ok((select not stale and folded_ops = 50200 from public.game_fold
            where game_uuid = 'b2222222-2222-4222-8222-222222222222')
          and (select s.xp = public.evolved_replay(s.game_uuid)->'xp_micro'
                 from public.game_state s
                where s.game_uuid = 'b2222222-2222-4222-8222-222222222222'),
          'the big upload took the fast path and equals the full replay');

select * from finish();
rollback;
