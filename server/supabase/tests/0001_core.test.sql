-- server/supabase/tests/0001_core.test.sql - pgTAP: constraints, RLS, helpers.
-- Run: cd server && supabase test db
-- NOTE: several pgTAP introspection helpers bind unknown literals to their
-- (table, description) overloads, so structural checks below query the
-- catalogs directly. throws_ok needs (sql, code, exact message, description).
begin;
select plan(19);

-- Tables exist (3-arg has_table binds the schema-qualified form; verified).
select has_table('public', 'players', 'players table exists');
select has_table('public', 'game_operations', 'game_operations table exists');
select has_table('public', 'game_state', 'game_state table exists');
select has_table('public', 'review_claims', 'review_claims table exists');
select has_table('public', 'rules_versions', 'rules_versions table exists');

-- RLS is active on private tables (direct catalog read, no overload games).
select ok((select relrowsecurity from pg_class
           where relname = 'players'
             and relnamespace = 'public'::regnamespace),
          'rls on players');
select ok((select relrowsecurity from pg_class
           where relname = 'game_operations'
             and relnamespace = 'public'::regnamespace),
          'rls on game_operations');
select ok((select relrowsecurity from pg_class
           where relname = 'game_state'
             and relnamespace = 'public'::regnamespace),
          'rls on game_state');

-- Uniqueness + not-null enforced structurally (not UI prechecks).
select ok(exists(select 1 from pg_indexes
                 where schemaname = 'public' and tablename = 'players'
                   and indexdef like '%(username_norm)%'),
          'username_norm unique');
select ok((select attnotnull from pg_attribute
           where attrelid = 'public.game_operations'::regclass
             and attname = 'op_id'),
          'op_id not null');

-- evolved_level() mirrors golden vectors (micro-XP in, level out).
select is(
  public.evolved_level(0, array[0,83,174,276]::bigint[]), 1,
  'level 1 at 0 XP');
select is(
  public.evolved_level(82000000, array[0,83,174,276]::bigint[]), 1,
  'level 1 at 82 XP');
select is(
  public.evolved_level(83000000, array[0,83,174,276]::bigint[]), 2,
  'level 2 at 83 XP');

-- Anonymous role is denied at the privilege level (RLS on + zero grants:
-- 42501, not silent empty sets). This is the stronger assertion.
set local role anon;
select throws_ok(
  $$ select count(*) from public.players $$,
  '42501', 'permission denied for table players',
  'anon selects on players denied');
select throws_ok(
  $$ select count(*) from public.game_operations $$,
  '42501', 'permission denied for table game_operations',
  'anon selects on operations denied');
reset role;

-- Validation guards raise before mutation.
select throws_ok(
  $$ select public.hiscores('smithing2', 10) $$,
  '22000', 'bad_skill',
  'hiscores rejects non-allowlisted skill');

-- link_game requires authentication.
set local role authenticated;
select throws_ok(
  $$ select public.link_game('11111111-1111-1111-1111-111111111111') $$,
  '28000', 'not_authenticated',
  'link_game without jwt sub raises');
reset role;

-- Seed two auth users + players as superuser for ownership tests.
insert into auth.users(id, aud, role, email, encrypted_password,
                       email_confirmed_at, created_at, updated_at)
values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '', 'authenticated',
        'a@example.com', crypt('pw123456', gen_salt('bf')),
        now(), now(), now()),
       ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '', 'authenticated',
        'b@example.com', crypt('pw123456', gen_salt('bf')),
        now(), now(), now())
on conflict (id) do nothing;
insert into public.players(user_id, username_norm, username_display)
values ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'alice', 'Alice'),
       ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'bob', 'Bob')
on conflict (user_id) do nothing;

-- Duplicate normalized username rejected (unique index, not UI precheck).
select throws_ok(
  $$ insert into public.players(user_id, username_norm, username_display)
     values ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'alice', 'Alice2') $$,
  '23505',
  'duplicate key value violates unique constraint "players_username_norm_ux"',
  'duplicate username_norm rejected');

-- Authenticated user cannot touch an unlinked game.
set local role authenticated;
select set_config('request.jwt.claims',
  '{"sub":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","role":"authenticated"}', true);
select throws_ok(
  $$ select public.get_game_state('22222222-2222-2222-2222-222222222222') $$,
  '42501', 'game_mismatch',
  'cross-account game_state raises');
select * from finish();
rollback;
