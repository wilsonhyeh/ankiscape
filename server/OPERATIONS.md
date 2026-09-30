# server/OPERATIONS.md - Supabase Free operations (contract F)

Backend: Supabase Free Auth + Postgres. Accept the inactivity pause and finite
storage. No new paid subscription is authorized. No keep-alive traffic to evade
pausing.

The daily backup job (see *Backups and rollback*) is not keep-alive traffic - it
exists so the data can be recovered - but it does connect to the database once a
day, so it registers as activity. That is a side effect, not a strategy. Do not
add traffic whose purpose is to defeat the pause.

## Local development (root orchestration, working directory `server/`)

```bash
cd server && supabase start        # local Auth + Postgres + captured mail
cd server && supabase db reset     # clean migration reset (run twice in tests)
cd server && supabase test db      # pgTAP: RLS/transaction/auth tests
```

Configure local-only Auth/email capture in `server/supabase/config.toml`.
Daily local dev never uses hosted projects, real SMTP, or AnkiWeb logins.

## Migrations

- `server/supabase/migrations/0001_evolved_core.sql` - tables, indexes,
  constraints, RLS revocation, `evolved_level()` helper.
- `server/supabase/migrations/0002_evolved_rpcs.sql` - `link_game`,
  `submit_operations`, `fetch_operations`, `get_game_state`, `hiscores`,
  `public_profile` (all `SECURITY DEFINER`, fixed `search_path`).
- `server/supabase/migrations/0003`-`0010` - registration gate, authoritative
  scoring, gem-inventory grant, test cohorts, fixture reservation emails,
  account lifecycle, public demo leaderboard, account-deletion cleanup.
- `server/supabase/migrations/0011_account_identity_rework.sql` - `link_game` returns the account's game (creates when absent; never `game_mismatch`/`game_claimed`), `players.visible_on_board`, `set_board_visibility`, `evolved_capabilities` `board_visibility`, `self_context` `visible_on_board` (all `SECURITY DEFINER`, fixed `search_path`).
- `server/supabase/migrations/0012_service_role_table_grants.sql` - grants
  `service_role` the table/sequence DML the postgres-owned default ACL for
  `public` withholds, and sets matching default privileges so later migrations
  inherit it. Without it `service_role` cannot write any migration-created
  table (PostgREST `403` / `42501`), which made the required `account-contracts`
  scenario un-passable on a `db reset` stack. `anon`/`authenticated` unchanged.

## Backups and rollback

**The database is backed up daily, and the backup is not the provider's.**
Supabase takes **no** backups on the Free plan — daily backups are
Pro/Team/Enterprise only — so until 2026-09-28 the hosted database was the only
copy of every account and all online progress. Two independent legs now cover
it:

- **Local** - `~/Documents/ankiscape-production-backups/` on Wilson's Mac,
  launchd job `com.wilsonyeh.ankiscape-backup`, daily 12:00 local, 30-day
  retention. Runs only when that machine is awake.
- **Off-site** - private repo `wilsonhyeh/ankiscape-backups`, GitHub Actions
  `database-backup.yml`, daily 08:17 UTC, 30-day retention.

Each run writes schema + data + roles as gzipped SQL with a sha256 manifest and
a row-count manifest, and **fails on a silently empty dump** rather than passing
green.

**Read the restore procedure before restoring.** Canonical copy:
`~/Documents/ankiscape-production-backups/RESTORE.md`. Two things in it are
load-bearing and were learned by running a drill, not by reading:

- A plain `psql -f data.sql` restore **produces the right row counts and the
  wrong data.** `public.players` is populated by a trigger on `auth.users`, and
  `pg_dump` emits `auth.*` before `public.*`, so the trigger creates the player
  rows first and the `public.players` COPY then dies on `players_pkey` - leaving
  the correct count with every `created_at` reset to the restore date. Prepend
  `SET session_replication_role = replica;` to the load.
- The superuser on a Supabase project is `supabase_admin`, not `postgres`.

**Migration rollback:** `supabase migration repair` + `supabase db reset` to the
target version, then restore data from the most recent backup **taken before**
the change you are undoing.

⚠️ **Corrected 2026-09-28.** This section previously read *"restore from
`supabase db dump` backups taken before each migration."* **No such backup was
ever taken** across migrations `0001`-`0012`, so the documented rollback named a
recovery source that did not exist. The daily schedule does not know a migration
is coming: **if you need a pre-migration restore point, take a dump first.**

## Capacity (Free tier, verify at deploy time)

- 500 MB database, 50,000 MAU, two active free projects, pause after one
  week of inactivity. Do not claim slots without checking at deploy time.
- Measure storage per 100,000 operations and replay cost; the backend-tests
  benchmark reports actual memory/time/database size.
- At capacity: stop online writes safely, retain local pending operations
  until capacity restores. No automatic paid upgrade.

## Rate limits (starting values)

- 60 API requests/minute/IP, 30 write batches/minute/account, with retry
  guidance (`Retry-After`). Upload/backlog limits are separate from
  earning-time anomaly checks.
- Suspicious earning patterns flag for review; network errors, backlogs,
  retries, material conflicts, and supported Undo never flag. No auto-ban.

## Maintainer CLI (local-only, privileged key via environment)

`server/maintainer.py` (planned): inspect/clear flags, ban/unban, export or
delete account data - always with confirmation. Privileged keys stay out of
the add-on, logs, and artifacts. Not yet implemented; do not hand-roll SQL
against production without it.

## Test cohort (migration 0006)

- `public.players.is_test` marks permanent hosted fixture accounts. Only
  privileged tooling (service role / SQL) may set it; authenticated requests
  are blocked by a guard trigger and client metadata is ignored.
- `public.fixture_registry` (private) reserves fixture usernames before any
  account exists, so a fixture player row is born classified.
- Public `hiscores`/`public_profile` filter `is_test = false` before rank,
  sort and limit, and (migration 0011) `players.visible_on_board = true`;
  test profiles are indistinguishable from unknown names.
- `test_hiscores`/`test_public_profile`/`self_context` are authenticated-only
  and authorize from the caller's own player row, never a client flag.
- **The demo players are removed from production (2026-09-29).** Do not run
  `dev/demo_players.py --hosted --apply`; it recreates them. The hosted lane
  runs `--verify-absent` (read-only) and fails if any reappear. The old
  hosted-v1 24-account suite is retired; `dev/seed_hosted_fixtures.py`
  refuses to recreate it. Never delete registry-owned accounts by hand.

## Public demo board (migration 0009)

> **Retired on production 2026-09-29.** The five demo players were deleted at
> the owner's request once real players began joining, and the nightly hosted
> lane stopped re-seeding them (it now runs `demo_players.py --hosted
> --verify-absent`). The `is_demo` column, the `[Demo]` label in the add-on and
> the local/dev tooling remain, so a demo row is always visibly labeled. What
> follows describes the mechanism, not current production contents.

- `public.players.is_demo` marks the five permanent public demo players
  (DemoWillow, DemoFlint, DemoMoss, DemoRowan, DemoCopper). Privileged tooling
  only, same guard pattern as `is_test`; never settable from client metadata
  or ordinary RPCs.
- Public `hiscores` returns exactly `{rank, username, xp, is_demo}` per row
  and `public_profile` exactly `{username, is_demo, state:{xp}}`. No auth or
  game UUIDs, inventory, checkpoints, counters or timestamps.
- Visible membership = active real players plus published demos; temporary
  test rows stay excluded; demos rank under the same rules (never pinned).
- Apply/verify: `python3 dev/demo_players.py --local|--hosted
  --plan|--apply --plan-file PATH|--verify|--verify-absent`. Every hosted
  deletion requires an exact fixture_registry/user/game/email match and
  stops on drift. **Do not `--apply` against production.**

## Overall board (migration 0013)

- `public.hiscores(p_skill, p_limit)` also accepts `p_skill = 'overall'`, ranking
  players by **total XP across the six skills**. Same row shape
  `{rank, username, xp, is_demo}`, same visibility rules and filter-before-rank
  as the per-skill boards; every per-skill result is unchanged and an unknown
  skill still raises `bad_skill`. Read-only: no table, grant or capability
  changes (only the private `_hiscores_public` helper is replaced).
- Ranked by XP, not total level, because levels come from the shared rules
  table client-side; duplicating it in SQL would add a second source of truth
  that scoring parity does not cover. The add-on derives Overall levels from
  the six skill boards.
- `test_hiscores` (the authenticated test cohort) does **not** support
  `overall`; the add-on hides the Overall tab in that mode. A server without
  0013 raises `bad_skill` for it, and the add-on hides the tab too.
- Tests: `server/supabase/tests/0008_overall_board.test.sql`.

## Account status (migration 0008)

- `public.account_lifecycle_status(email, username)` is service-role only and
  returns `{email_status: new|unconfirmed|confirmed, username_available}`;
  it reads auth.users but exposes no ids, stored emails or metadata.
  `public.account_status_consume` provides atomic fixed-window rate buckets
  keyed by HMAC digests supplied by the `account-status` Edge Function.
- Deploy: `supabase functions deploy account-status --no-verify-jwt` plus the
  updated `username-login`; set `ACCOUNT_STATUS_HMAC_SECRET`. Neither function
  creates accounts or sends mail.

## Account deletion (migration 0010 + `account-delete`)

- `0010_account_deletion_cleanup.sql` adds a narrowly scoped `BEFORE DELETE`
  trigger on `public.players`: deleting one player row removes that game's
  `game_checkpoints` and that user's `moderation_audit` rows in the same
  transaction as the Auth deletion. Auth cascades cover operations, review
  claims and `game_state`. No broad deletes, no orphan sweeps. A failed Auth
  deletion or the `fixture_registry` RESTRICT rolls the cleanup back.
- Deploy order: `supabase db push --dry-run --skip-vault --project-ref <ref>`
  (expect exactly the new cleanup migration), then
  `supabase db push --skip-vault --project-ref <ref>`, then
  `supabase functions deploy account-delete --project-ref <ref>` with JWT
  verification ON (config.toml sets `[functions.account-delete] verify_jwt =
  true`). Never use a global `--no-verify-jwt`; never redeploy 0008/0009.
- Verified on the 2026-09-14 hosted deploy: a request with an invalid bearer
  JWT is rejected at the edge (401 `Invalid JWT`), while an apikey-only
  request without any Authorization header still reaches the function — which
  fails closed with 401 `invalid_session` before reading any data. Both the
  edge check and the function's own session proof are load-bearing.
- Scope: the server account, game progress, scores and leaderboard presence are
  removed from the live database. Local progress is kept unless the client opts
  in to remove it on that computer.
- ⚠️ **Backup tail (added 2026-09-28).** Deletion is immediate and complete in
  the live database, but it cannot reach the backups: a snapshot taken before
  the deletion still contains the account for up to 30 days, on both legs (local
  and `wilsonhyeh/ankiscape-backups`). The client copy says the account is
  removed *"permanently"* and *"this cannot be undone"*
  (`evolved/ui/account.py:329,347`), which is true of the live service and not
  strictly true of retained snapshots. **Whether that wording changes is
  Wilson's call, not an agent's - do not silently reword it.** Provider backups
  and provider operational logs remain out of scope, and on the Free plan there
  are none.
- Rollout failure: stop and do not ship the client delete entry point against
  a server without the cleanup migration. An authorized rollback may disable
  the endpoint/client entry while keeping the additive migration; it cannot
  restore a deleted account. Issued access JWTs stay cryptographically valid
  until expiry, so deletion is proven by the Auth user being absent and new
  logins/link attempts failing, not by every endpoint returning 401.
- Recovery for an unknown client outcome: the client keeps a credential-free
  `account-deletion.json` marker, suspends online work for that identity and
  offers a read-only status check. Only an authoritative user-not-found
  result establishes absence; a confirmed existing account may retry with
  fresh confirmation.
