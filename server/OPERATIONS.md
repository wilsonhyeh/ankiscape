# server/OPERATIONS.md - Supabase Free operations (contract F)

Backend: Supabase Free Auth + Postgres. Accept the inactivity pause and finite
storage. No new paid subscription is authorized. No keep-alive traffic to evade
pausing.

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

Rollback: `supabase migration repair` + `supabase db reset` to the target
version; restore from `supabase db dump` backups taken before each migration.

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
  sort and limit; test profiles are indistinguishable from unknown names.
- `test_hiscores`/`test_public_profile`/`self_context` are authenticated-only
  and authorize from the caller's own player row, never a client flag.
- Seed/verify: `python3 dev/demo_players.py --hosted --apply|--verify`
  (see docs/RELIABILITY.md for credentials and rate limits). The old
  hosted-v1 24-account suite is retired; `dev/seed_hosted_fixtures.py`
  refuses to recreate it. Never delete registry-owned accounts.

## Public demo board (migration 0009)

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
  --plan|--apply --plan-file PATH|--verify`. Every hosted deletion requires
  an exact fixture_registry/user/game/email match and stops on drift.

## Account status (migration 0008)

- `public.account_lifecycle_status(email, username)` is service-role only and
  returns `{email_status: new|unconfirmed|confirmed, username_available}`;
  it reads auth.users but exposes no ids, stored emails or metadata.
  `public.account_status_consume` provides atomic fixed-window rate buckets
  keyed by HMAC digests supplied by the `account-status` Edge Function.
- Deploy: `supabase functions deploy account-status --no-verify-jwt` plus the
  updated `username-login`; set `ACCOUNT_STATUS_HMAC_SECRET`. Neither function
  creates accounts or sends mail.
