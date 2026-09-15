# AnkiScape 3.0.0 — known limitations (pre-publication)

Drafted during release preparation. Nothing here is a claim of permission,
support, or a promise; unresolved items block publication until Wilson
decides otherwise.

## Account and sync

- The sync backend runs on Supabase Free. After long inactivity it pauses;
  online writes then stop safely and local pending operations are kept.
- One account links one Evolved game. Concurrent offline play on two desktops
  merges via the journal; conflicting ingredient use resolves to zero reward
  for the losing action (policy 2).
- Real-inbox email delivery (registration/recovery codes via
  `ankiscape@ankiscape.xyz`) has not been verified by a human yet. Hosted
  automation uses admin-generated codes and therefore does not prove inbox
  delivery.
- Hiscores are per-skill, username + XP only. No friends, clans, or web
  hiscores.
- Evolved starts fresh: Classic levels, XP and items are never transferred.
  There is no Classic-to-Evolved transfer, and the first switch shows a
  fresh-start acknowledgement. Each mode keeps its own progress.
- Account deletion removes the server account and its online data (game
  progress, backups, scores and leaderboard presence). It does not promise
  erasure of provider backups or provider operational logs, and shared
  short-lived anti-abuse buckets may retain digests. Local Evolved progress
  is kept unless the user opts in to remove it on that computer.

## Platform

- Verified in CI/local on macOS 26.08.1 and 23.10.1; Linux and Windows lanes
  are part of the release gate and must be green before publication.
- No phone game UI: phones are review devices; rewards arrive by desktop
  catch-up.
- No telemetry. Diagnostics are user-initiated and allowlisted.

## Content and rights

- Artwork is bundled Old School RuneScape Wiki art used in a noncommercial fan
  project. `docs/ASSET-RIGHTS.md` records that redistribution permission is
  unresolved; publication requires Wilson's explicit decision and is blocked
  until then.
- No trading, equipment upgrades, quests, or social features in 3.0.

## Mobile

- AnkiMobile/AnkiDroid catch-up, Undo-before-import, and phone-deletion
  reconciliation are listed as human checks in `RELEASE-SMOKE.md` and have
  not been performed yet.

## Performance

- After a late retraction with a very large review history (~100k game
  operations), the projection worker replays the history and the interface
  can stall up to ~3.7 s at worst observed on the slowest supported runner
  class (~0.9-1.2 s typical on Linux/Windows). Rewards waiting on that
  projection appear when it completes. Ordinary reviews are unaffected:
  accepted-answer p50 is ~102 ms and event-loop lag p95 stays under 50 ms
  on Linux/Windows. Moving the projection to a separate process is planned
  stabilization work, not a 3.0 change; the stall is bounded by the
  rebuild-window budgets in `dev/reliability-budgets.json`, not fixed.

## Recovery

- Reinstalling 2.0.2 does not restore Evolved progress; see
  `docs/release/3.0.0/ROLLBACK.md` for what to preserve before any downgrade.
