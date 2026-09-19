-- 0012_service_role_table_grants.sql - grant service_role the table DML it is
-- documented to have. Append-only: never edit 0001-0011 as a rollout strategy.
--
-- Why this exists: the postgres-owned default ACL for schema `public` grants
-- service_role only REFERENCES, TRIGGER and TRUNCATE, and it wins over the
-- supabase_admin default that carries the full DML set. A freshly created table
-- inherits the same narrow ACL, so NO table created by a migration is writable
-- as service_role on a stack built with `supabase db reset`:
--
--   POST /rest/v1/game_checkpoints  ->  HTTP 403
--   {"code":"42501","message":"permission denied for table game_checkpoints"}
--
-- That is not RLS and not stale state, and it is not caused by any one
-- migration. It made `account-contracts` - required at pr, nightly and release -
-- structurally un-passable in every environment that builds the database from
-- migrations, because its fixture seeding writes through service_role.
--
-- Scope: service_role only. `anon` and `authenticated` are deliberately
-- untouched; widening either changes what a client can reach, which is a
-- product security decision and not what this fix is for.

grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Tables and sequences created by later migrations inherit the same grants, so
-- this cannot silently regress on the next migration.
alter default privileges in schema public
  grant select, insert, update, delete on tables to service_role;
alter default privileges in schema public
  grant usage, select on sequences to service_role;
