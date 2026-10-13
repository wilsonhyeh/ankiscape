# Incident / rollback note — AnkiScape 3.0.3

Preparation document; not an authorization to publish or to downgrade.

## Why this is a small rollback

3.0.3 changes only three UI files (`evolved/ui/skills.py`, `widgets.py`,
`training.py`): which button label and badges the Skills and Training screens
show. No journal format, scoring, account, sync-protocol or server change.
3.0.2 and 3.0.3 clients are interchangeable against the same server.

## Stop distribution

1. **Primary lever: do not upload to AnkiWeb (or revert the listing).** Wilson
   performs this; agents do not publish. Revert the listing to 3.0.2
   (`ankiscape-3.0.2.ankiaddon`, sha256 `b5ea6f7d…`, GitHub release `v3.0.2`).
2. Keep the 3.0.3 candidate archive, its manifest and the release-verify
   evidence run; do not delete failing evidence.
3. Do not delete hosted data or reset production.

## Downgrading a user from 3.0.3 to 3.0.2

- Install `ankiscape-3.0.2.ankiaddon` over it. Progress is untouched; 3.0.3
  writes nothing 3.0.2 does not. The switch-back bug comes back.
- The 3.0.0 recovery notes apply unchanged; see
  `docs/release/3.0.0/ROLLBACK.md`.
