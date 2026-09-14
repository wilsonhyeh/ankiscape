# Incident / rollback note — AnkiScape 3.0.0

Preparation document. It describes what to do if 3.0.0 must be stopped or a
user needs to recover; it is not an authorization to publish or to downgrade.

## Stop distribution

1. On AnkiWeb, revert the listing to the last known-good version (2.0.2) or
   hide the 3.0.0 update. Wilson performs this; agents do not publish.
2. Keep the verified candidate archive, its manifest and the release-verify
   evidence run (do not delete the failing evidence).
3. Do not delete hosted data or reset production. Keep the schema
   backward-compatible with 2.0.2 clients (the exposed RPCs still gate older
   clients with `client_update_required`).

## Preserve Evolved data before any downgrade

- Evolved progress lives in a journal next to the Anki profile at
  `<profile>/ankiscape-evolved/<game>/game.sqlite3` (plus its outbox).
- Before installing 2.0.2 over 3.0.0, copy that directory and the account
  backup export (`Settings → Advanced → Export Game Backup`) somewhere safe.
- **Reinstalling 2.0.2 does not restore Evolved progress** and does not
  remove it. 2.0.2 never reads or deletes the Evolved journal.
- A linked account can rebuild from the server (stored operations) plus the
  preserved outbox; an anonymous game needs its backup file.

## Reproduce backup restore

1. In an isolated profile, restore from the exported backup
   (`Settings → Advanced → Restore Game Backup`) and verify level/XP/Bank.
2. Repeat with a copy of the raw journal directory for the account path.
3. Validate on a disposable profile only; never reset production or reverse
   hosted cohort filters to "fix" a client.

## Verify recovery

- The restored state must equal the reference reducer over the same
  operations (`engine.projection()` boundary: xp_micro, inventory, levels,
  revision).
- Pending outbox operations upload after reconnecting; duplicates are
  acknowledged idempotently.
