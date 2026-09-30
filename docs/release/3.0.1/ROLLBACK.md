# Incident / rollback note — AnkiScape 3.0.1

Preparation document; not an authorization to publish or to downgrade.

## Why this is a small rollback

3.0.1 changes only the Hiscores screen on the 3.0.0 base. There is no client
data migration: game journals, accounts and sync are byte-for-byte the 3.0.0
behavior. The one server change, migration `0013`, only widens the read-only
`hiscores()` function to accept `overall`; it changes no existing result, so
**3.0.0 clients keep working against the migrated server.** Do not revert `0013`
to roll back a client defect.

## Stop distribution

1. **Primary lever: do not upload to AnkiWeb (or revert the listing).** Wilson
   performs this; agents do not publish. Revert the listing to 3.0.0
   (`ankiscape-3.0.0.ankiaddon`, sha256 `c22876fe…`, GitHub release `v3.0.0`).
2. Keep the 3.0.1 candidate archive, its manifest and the release-verify
   evidence run; do not delete failing evidence.
3. Do not delete hosted data or reset production.

## Downgrading a user from 3.0.1 to 3.0.0

- Install `ankiscape-3.0.0.ankiaddon` over it. Progress is untouched: 3.0.1
  adds no journal format.
- 3.0.1 leaves one extra file in the profile folder,
  `ankiscape_hiscores_seen.json` (public board ranks only). 3.0.0 ignores it;
  it can be deleted.
- The 3.0.0 recovery notes (backup export, journal location) apply unchanged;
  see `docs/release/3.0.0/ROLLBACK.md`.
