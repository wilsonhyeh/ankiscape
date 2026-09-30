-- server/supabase/tests/0008_overall_board.test.sql - Overall Hiscores board.
-- Run: cd server && supabase test db
-- Covers migration 0013: p_skill = 'overall' ranks by total XP across the six
-- skills, keeps the allowlisted row shape, leaves every per-skill board
-- unchanged, and unknown skills still raise bad_skill.
begin;
select plan(8);

insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at,
                       raw_user_meta_data)
values
  ('50000000-0000-4000-8000-000000000001', '', 'authenticated',
   'spread@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"spread","username_display":"Spread"}'),
  ('50000000-0000-4000-8000-000000000002', '', 'authenticated',
   'spike@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"spike","username_display":"Spike"}'),
  ('50000000-0000-4000-8000-000000000003', '', 'authenticated',
   'idle@example.invalid', crypt('pw123456', gen_salt('bf')),
   now(), now(), now(),
   '{"username_norm":"idle","username_display":"Idle"}')
on conflict (id) do nothing;

update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000001'
 where username_norm = 'spread';
update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000002'
 where username_norm = 'spike';
update public.players set game_uuid = 'dddddddd-0000-4000-8000-000000000003'
 where username_norm = 'idle';

-- Spread: 4M in each of three skills (12M total, best single skill 4M).
-- Spike: 9M in one skill (9M total, best single skill 9M).
-- Idle: no XP at all (empty xp object).
insert into public.game_state(game_uuid, user_id, xp, revision)
select p.game_uuid, p.user_id,
       case p.username_norm
         when 'spread' then jsonb_build_object(
           'mining', 4000000, 'woodcutting', 4000000, 'fishing', 4000000)
         when 'spike' then jsonb_build_object('mining', 9000000)
         else '{}'::jsonb end,
       1
  from public.players p
 where p.username_norm in ('spread', 'spike', 'idle');

set local role anon;

select is(
  (select r ->> 'username' from jsonb_array_elements(
     public.hiscores('overall', 10)) r
    where (r ->> 'rank')::int = 1),
  'Spread',
  'overall ranks by total XP: the all-rounder beats the single-skill spike');
select is(
  (select (r ->> 'xp')::bigint from jsonb_array_elements(
     public.hiscores('overall', 10)) r where r ->> 'username' = 'Spread'),
  12000000::bigint, 'overall xp is the sum of the six skills');
select is(
  (select (r ->> 'xp')::bigint from jsonb_array_elements(
     public.hiscores('overall', 10)) r where r ->> 'username' = 'Idle'),
  0::bigint, 'a player with no XP is present with 0');
select is(
  (select array_agg(k order by k)::text[] from jsonb_object_keys(
     (public.hiscores('overall', 10) -> 0)) k),
  array['is_demo', 'rank', 'username', 'xp']::text[],
  'overall rows keep the allowlisted shape');

-- Per-skill boards are exactly as before: Spike leads mining.
select is(
  (select r ->> 'username' from jsonb_array_elements(
     public.hiscores('mining', 10)) r where (r ->> 'rank')::int = 1),
  'Spike', 'mining board still ranks by mining XP alone');
select is(
  (select (r ->> 'xp')::bigint from jsonb_array_elements(
     public.hiscores('fishing', 10)) r where r ->> 'username' = 'Spread'),
  4000000::bigint, 'fishing board still reports fishing XP alone');

-- Limit and validation behave as they did.
select is(
  jsonb_array_length(public.hiscores('overall', 1)), 1,
  'the limit applies to the overall board');
select throws_ok(
  $$ select public.hiscores('farming', 10) $$,
  '22000', 'bad_skill', 'unknown skills are still rejected');
reset role;

select * from finish();
rollback;
