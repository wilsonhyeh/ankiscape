-- server/supabase/tests/0009_replay_scale.test.sql - Replay at real history
-- sizes (issue #63, migration 0014).
-- Run: cd server && supabase test db
-- submit_operations replays the whole history on every upload. The 0005
-- replay was quadratic and ran past the authenticated role's 8 s
-- statement_timeout at ~3,000 operations (PostgREST 57014), so a long-time
-- player could never upload again. This seeds 6,000 operations (the 0005 body
-- needs ~13 s here) and submits a full 200-op batch as `authenticated` under
-- the same 8 s limit.
begin;
select plan(6);

insert into auth.users(id, instance_id, aud, role, email, encrypted_password,
  email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at,
  updated_at, confirmation_token, recovery_token, email_change_token_new,
  email_change, email_change_token_current, phone_change, phone_change_token,
  reauthentication_token)
values ('77777777-7777-4777-8777-777777777777',
        '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 'pgscale@example.invalid', 'x', now(), '{}'::jsonb,
        '{"username_norm":"pgscale","username_display":"pgscale"}'::jsonb,
        now(), now(), '', '', '', '', '', '', '', '')
on conflict (id) do nothing;

update public.players set game_uuid = '88888888-8888-4888-8888-888888888888'
 where user_id = '77777777-7777-4777-8777-777777777777';
insert into public.game_state(game_uuid, user_id)
values ('88888888-8888-4888-8888-888888888888',
        '77777777-7777-4777-8777-777777777777')
on conflict (game_uuid) do nothing;

-- A long mixed history: direct gathering/smithing, catch-up awards, skips,
-- and retract/restore pairs.
create temp table scale_ops on commit drop as
select i,
  case when i % 25 = 0 then 'review_retract'
       when i % 25 = 1 and i > 25 then 'review_restore'
       when i % 40 = 7 then 'review_skip'
       else 'review_award' end as kind,
  case when i % 25 = 0 then jsonb_build_object('target_review_key', 'rk-' || (i - 3))
       when i % 25 = 1 and i > 25 then jsonb_build_object('target_review_key', 'rk-' || (i - 4))
       when i % 40 = 7 then jsonb_build_object('review_key', 'rk-' || (i + 1),
         'reason', 'classic_mode', 'provenance', 'skip')
       when i % 5 = 0 then jsonb_build_object('review_key', 'rk-' || i,
         'review_ts', 1000 + i, 'provenance', 'catchup', 'reward_policy', 2)
       else jsonb_build_object('review_key', 'rk-' || i, 'review_ts', 1000 + i,
         'provenance', 'direct', 'reward_policy', 2,
         'skill', (array['mining','mining','smithing','woodcutting'])[1 + i % 4],
         'resource', (array['Copper ore','Tin ore','Bronze bar','Tree'])[1 + i % 4])
  end as payload
from generate_series(1, 6200) as i;

insert into public.game_operations(op_id, game_uuid, user_id, device_id,
  device_seq, lamport, kind, payload, payload_hash)
select md5('pgscale-' || i)::uuid, '88888888-8888-4888-8888-888888888888',
       '77777777-7777-4777-8777-777777777777', 'dev-a', i, i, kind, payload, ''
  from scale_ops where i <= 6000;
-- submit_operations below reads the batch as `authenticated`.
grant select on scale_ops to authenticated;

select is(
  (public.evolved_replay('88888888-8888-4888-8888-888888888888')->>'revision')::int,
  6000, 'replay covers the full 6,000-operation history');

-- Every retract at op i targets rk-(i-3) and op i+1 restores it. rk-97 is
-- retracted at 100 and restored at 101. rk-5997 is retracted at 6000; its
-- restore (6001) only arrives in the batch submitted below.
select isnt(
  public.evolved_replay('88888888-8888-4888-8888-888888888888')
    ->'review_outcomes'->'rk-97',
  null, 'a restored review is back in the outcomes');
select is(
  public.evolved_replay('88888888-8888-4888-8888-888888888888')
    ->'review_outcomes'->'rk-5997',
  null, 'a retracted review stays out of the outcomes');

-- The production path: a full batch, as authenticated, under its timeout.
select set_config('request.jwt.claims',
  '{"sub":"77777777-7777-4777-8777-777777777777","role":"authenticated"}', true);
set local role authenticated;
set local statement_timeout = '8s';
select lives_ok(
  $$ select public.submit_operations(
       '88888888-8888-4888-8888-888888888888',
       (select jsonb_agg(jsonb_build_object(
          'op_id', md5('pgscale-' || i)::uuid, 'device_id', 'dev-a',
          'device_seq', i, 'lamport', i, 'kind', kind, 'payload', payload)
          order by i)
          from scale_ops where i > 6000)) $$,
  'a 200-op batch on a 6,000-op history finishes inside the 8 s statement_timeout');
reset statement_timeout;
reset role;

select is(
  (select count(*)::int from public.game_operations
    where game_uuid = '88888888-8888-4888-8888-888888888888'),
  6200, 'the whole batch landed');
select isnt(
  public.evolved_replay('88888888-8888-4888-8888-888888888888')
    ->'review_outcomes'->'rk-5997',
  null, 'the submitted restore re-scored the retracted review');

select * from finish();
rollback;
