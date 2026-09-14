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

## Recovery

- Reinstalling 2.0.2 does not restore Evolved progress; see
  `docs/release/3.0.0/ROLLBACK.md` for what to preserve before any downgrade.
