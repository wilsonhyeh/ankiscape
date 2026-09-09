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
