# Incident / rollback note — AnkiScape 3.0.2

Preparation document; not an authorization to publish or to downgrade.

## Why this is a small rollback

3.0.2 changes only where AnkiScape's network work runs (which thread). No
journal format, account, sync-protocol or server change. 3.0.1 and 3.0.2
clients are interchangeable against the same server.

## Stop distribution

1. **Primary lever: do not upload to AnkiWeb (or revert the listing).** Wilson
   performs this; agents do not publish. Revert the listing to 3.0.1
   (`ankiscape-3.0.1.ankiaddon`, sha256 `31eb1b58…`, GitHub release `v3.0.1`).
2. Keep the 3.0.2 candidate archive, its manifest and the release-verify
   evidence run; do not delete failing evidence.
3. Do not delete hosted data or reset production.

## Downgrading a user from 3.0.2 to 3.0.1

- Install `ankiscape-3.0.1.ankiaddon` over it. Progress is untouched; 3.0.2
  writes nothing 3.0.1 does not. The "Processing..." popup and the Sync freeze
  come back.
- The 3.0.0 recovery notes apply unchanged; see
  `docs/release/3.0.0/ROLLBACK.md`.
