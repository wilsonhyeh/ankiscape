-- server/supabase/tests/0012_operation_archive.test.sql - operation archive
-- (migration 0018).
-- Run: cd server && supabase test db
-- Compaction moves old operations into game_operation_segments. Nothing a
-- client or Hiscores can see may change: every fetch_operations page stays
-- byte-identical, evolved_replay and game_state stay identical, retries of
-- archived operations stay idempotent, uploads keep matching the full
-- replay, and account deletion still removes everything.
begin;
select plan(21);

insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
  email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at,
  updated_at, confirmation_token, recovery_token, email_change_token_new,
  email_change, email_change_token_current, phone_change, phone_change_token,
  reauthentication_token)
values ('c1111111-1111-4111-8111-111111111111',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgarchive@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgarchive","username_display":"pgarchive"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', ''),
       ('c2222222-2222-4222-8222-222222222222',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgarchivenew@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgarchivenew","username_display":"pgarchivenew"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', '')
on conflict (id) do nothing;

update public.players set game_uuid = 'd1111111-1111-4111-8111-111111111111'
 where user_id = 'c1111111-1111-4111-8111-111111111111';
update public.players set game_uuid = 'd2222222-2222-4222-8222-222222222222'
 where user_id = 'c2222222-2222-4222-8222-222222222222';
insert into public.game_state(game_uuid, user_id)
values ('d1111111-1111-4111-8111-111111111111', 'c1111111-1111-4111-8111-111111111111'),
       ('d2222222-2222-4222-8222-222222222222', 'c2222222-2222-4222-8222-222222222222')
on conflict (game_uuid) do nothing;

-- 5,500 eight-day-old operations: a catch-up preset (so archived presets must
-- still steer catch-up awards), direct and catch-up awards, skips, and
-- retract/restore pairs.
create function pg_temp.env(i int) returns jsonb language sql as $$
  select jsonb_build_object('op_id', md5('pgarchive-' || i)::uuid,
    'device_id', 'dev-a', 'device_seq', i, 'lamport', i,
    'kind', case when i = 3 then 'catchup_preset'
                 when i % 25 = 0 then 'review_retract'
                 when i % 25 = 1 and i > 25 then 'review_restore'
                 when i % 40 = 7 then 'review_skip'
                 else 'review_award' end,
    'payload', case
       when i = 3 then jsonb_build_object('skill', 'woodcutting', 'effective_ts', 500)
       when i % 25 = 0 then jsonb_build_object('target_review_key', 'rk-' || (i - 3))
       when i % 25 = 1 and i > 25 then jsonb_build_object('target_review_key', 'rk-' || (i - 4))
       when i % 40 = 7 then jsonb_build_object('review_key', 'rk-' || (i + 1),
         'reason', 'classic_mode', 'provenance', 'skip')
       when i % 5 = 0 then jsonb_build_object('review_key', 'rk-' || i,
         'review_ts', 1000 + i, 'provenance', 'catchup', 'reward_policy', 2)
       else jsonb_build_object('review_key', 'rk-' || i, 'review_ts', 1000 + i,
         'provenance', 'direct', 'reward_policy', 2,
         'skill', (array['mining','mining','smithing','woodcutting'])[1 + i % 4],
         'resource', (array['Copper ore','Tin ore','Bronze bar','Tree'])[1 + i % 4])
    end)
$$;

insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash, created_at)
select (e->>'op_id')::uuid, 'd1111111-1111-4111-8111-111111111111',
       'c1111111-1111-4111-8111-111111111111', 'dev-a', i, i, e->>'kind', e->'payload',
       encode(extensions.digest((e->'payload')::text::bytea, 'sha256'), 'hex'),
       now() - interval '8 days' + i * interval '1 second'
  from (select i, pg_temp.env(i) as e from generate_series(1, 5500) as i) s;
select public._evolved_fold_rebuild('d1111111-1111-4111-8111-111111111111', false);

create function pg_temp.as_user(u text) returns void language plpgsql as $$
begin
  perform set_config('request.jwt.claims',
    json_build_object('sub', u, 'role', 'authenticated')::text, true);
  set local role authenticated;
end $$;
-- Every fetch_operations page from cursor 0 (limit 200), as the owner.
create function pg_temp.pages() returns jsonb language plpgsql as $$
declare
  v_cur bigint := 0;
  v_page jsonb;
  v_all jsonb := '[]'::jsonb;
begin
  perform pg_temp.as_user('c1111111-1111-4111-8111-111111111111');
  loop
    v_page := public.fetch_operations('d1111111-1111-4111-8111-111111111111', v_cur, 200);
    exit when jsonb_array_length(v_page->'operations') = 0;
    v_all := v_all || jsonb_build_array(v_page);
    v_cur := (v_page->>'next_cursor')::bigint;
  end loop;
  reset role;
  return v_all;
end $$;
create function pg_temp.submit(ops jsonb) returns jsonb language plpgsql as $$
declare r jsonb;
begin
  perform pg_temp.as_user('c1111111-1111-4111-8111-111111111111');
  r := public.submit_operations('d1111111-1111-4111-8111-111111111111', ops);
  reset role;
  return r;
end $$;
create function pg_temp.state() returns jsonb language sql as $$
  select jsonb_build_object('xp', xp, 'inventory', inventory, 'counters', counters,
                            'achievements', achievements, 'checkpoint', checkpoint)
    from public.game_state where game_uuid = 'd1111111-1111-4111-8111-111111111111'
$$;
create function pg_temp.matches() returns boolean language sql as $$
  select s.xp = r->'xp_micro' and s.inventory = r->'inventory'
     and s.counters = r->'counters' and s.achievements = r->'achievements'
     and s.checkpoint->'levels' = r->'levels'
     and s.checkpoint->'diagnostics' = (select coalesce(jsonb_agg(d order by d), '[]'::jsonb)
                                         from jsonb_array_elements_text(r->'diagnostics') d)
    from public.game_state s,
         (select public.evolved_replay('d1111111-1111-4111-8111-111111111111') as r) x
   where s.game_uuid = 'd1111111-1111-4111-8111-111111111111'
$$;

create temp table before_compaction on commit drop as
select pg_temp.pages() as pages,
       public.evolved_replay('d1111111-1111-4111-8111-111111111111') as replay,
       pg_temp.state() as state;

-- 1. A stale fold is never compacted.
update public.game_fold set stale = true, stale_since = now()
 where game_uuid = 'd1111111-1111-4111-8111-111111111111';
select is(public._evolved_compact_game('d1111111-1111-4111-8111-111111111111'), 0,
          'a game whose fold is stale is skipped');
update public.game_fold set stale = false, stale_since = null
 where game_uuid = 'd1111111-1111-4111-8111-111111111111';

-- 2. Compaction: the newest 2,000 stay live, so 3,500 are eligible: three
--    whole segments, 500 leftovers stay live.
select is(public._evolved_compact_game('d1111111-1111-4111-8111-111111111111'), 3000,
          'compaction archives three whole 1,000-op segments');
select is((select count(*)::int from public.game_operation_segments
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 3,
          'three segments are written');
select is((select count(*)::int from public.game_operations
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 2500,
          '2,500 operations stay live (the newest 2,000 plus 500 leftovers)');
select is((select preset_count from public.game_operation_segments
            where game_uuid = 'd1111111-1111-4111-8111-111111111111' order by first_id limit 1), 1,
          'the segment holding the preset counts it');
select is((select max_seq from public.game_compaction_marks
            where game_uuid = 'd1111111-1111-4111-8111-111111111111' and device_id = 'dev-a'), 3000::bigint,
          'the compaction mark is the highest archived device_seq');
select is((select count(*)::int from public.game_operations_all
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 5500,
          'game_operations_all still holds all 5,500 operations');

-- 3. Nothing visible changed.
select is(pg_temp.pages()::text, (select pages::text from before_compaction),
          'every fetch_operations page is byte-identical after compaction');
select is(public.evolved_replay('d1111111-1111-4111-8111-111111111111')::text,
          (select replay::text from before_compaction),
          'evolved_replay is identical after compaction');
select public._evolved_fold_rebuild('d1111111-1111-4111-8111-111111111111', false);
select is(pg_temp.state()::text, (select state::text from before_compaction),
          'a full fold rebuild over live + archive leaves game_state unchanged');
select is((select folded_ops::int from public.game_fold
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 5500,
          'the rebuilt fold counts archived operations');
select is(public._evolved_compact_game('d1111111-1111-4111-8111-111111111111'), 0,
          'a second compaction finds no whole segment left to archive');

-- 4. Retries of archived operations.
select is(pg_temp.submit(jsonb_build_array(pg_temp.env(10))),
          jsonb_build_object('accepted', jsonb_build_array(pg_temp.env(10)->>'op_id'),
                             'conflicts', '[]'::jsonb, 'applied', 0),
          'an unchanged archived operation is accepted and not applied again');
select is(pg_temp.submit(jsonb_build_array(
            jsonb_set(pg_temp.env(11), '{payload,review_ts}', '1'))),
          jsonb_build_object('accepted', '[]'::jsonb,
                             'conflicts', jsonb_build_array(jsonb_build_object(
                               'op_id', pg_temp.env(11)->>'op_id', 'error', 'id_conflict')),
                             'applied', 0),
          'a changed archived operation is an id_conflict');
select is((select count(*)::int from public.game_operations_all
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 5500,
          'neither retry inserted a row');

-- 5. Uploads after compaction: a catch-up award (preset is archived) takes
--    the fast path; undo of an archived winner falls back to a rebuild.
update public.game_fold set rebuilt_at = '2000-01-01'
 where game_uuid = 'd1111111-1111-4111-8111-111111111111';
select pg_temp.submit(jsonb_build_array(jsonb_build_object(
  'op_id', md5('pgarchive-5505')::uuid, 'device_id', 'dev-a', 'device_seq', 5505,
  'lamport', 5505, 'kind', 'review_award',
  'payload', jsonb_build_object('review_key', 'rk-5505', 'review_ts', 9000,
    'provenance', 'catchup', 'reward_policy', 2))));
select ok((select rebuilt_at = '2000-01-01' from public.game_fold
            where game_uuid = 'd1111111-1111-4111-8111-111111111111') and pg_temp.matches(),
          'a catch-up award after compaction takes the fast path and matches the replay');
select pg_temp.submit(jsonb_build_array(jsonb_build_object(
  'op_id', md5('pgarchive-5506')::uuid, 'device_id', 'dev-a', 'device_seq', 5506,
  'lamport', 5506, 'kind', 'review_retract',
  'payload', '{"target_review_key":"rk-12","reason":"anki_undo"}'::jsonb)));
select ok((select stale from public.game_fold
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'),
          'undo of an archived winner on a large game marks the fold stale');
select public._evolved_fold_rebuild_stale(5);
select ok(pg_temp.matches(), 'after the background rebuild, game_state equals the replay');

-- 6. Operations newer than 7 days are never archived.
insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash)
select md5('pgarchivenew-' || i)::uuid, 'd2222222-2222-4222-8222-222222222222',
       'c2222222-2222-4222-8222-222222222222', 'dev-a', i, i, 'review_award',
       jsonb_build_object('review_key', 'rk-' || i, 'review_ts', 1000 + i,
         'provenance', 'direct', 'reward_policy', 2, 'skill', 'mining',
         'resource', 'Copper ore'), ''
  from generate_series(1, 4500) as i;
select public._evolved_fold_rebuild('d2222222-2222-4222-8222-222222222222', false);
select is(public._evolved_compact_game('d2222222-2222-4222-8222-222222222222'), 0,
          'operations newer than 7 days stay live even 2,000+ positions back');

-- 7. Account deletion removes the archive with everything else.
delete from public.players where user_id = 'c1111111-1111-4111-8111-111111111111';
select is((select count(*)::int from public.game_operation_segments
            where game_uuid = 'd1111111-1111-4111-8111-111111111111')
        + (select count(*)::int from public.game_compaction_marks
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 0,
          'segments and compaction marks are deleted with the player');
select is((select count(*)::int from public.game_fold
            where game_uuid = 'd1111111-1111-4111-8111-111111111111')
        + (select count(*)::int from public.game_fold_snapshots
            where game_uuid = 'd1111111-1111-4111-8111-111111111111')
        + (select count(*)::int from public.game_review_keys
            where game_uuid = 'd1111111-1111-4111-8111-111111111111'), 0,
          'fold, snapshots and review keys are deleted with the player');

select * from finish();
rollback;
