# Incident / rollback note — AnkiScape 3.0.0

Preparation document. It records the settled rollback rule and describes what
to do if 3.0.0 must be stopped or a user needs to recover; it is not an
authorization to publish or to downgrade. The backup-restore rehearsal below
has **not** been performed — those steps are written but unexercised, and
`READINESS.md` carries that row as pending.

## Settled rule — `0011` applied hosted, then verification fails

Settled; do not improvise a different order under release pressure.

1. **The primary lever is not publishing.** 3.0.0 is unpublished (AnkiWeb
   listing `1808450369` still serves 2.0.2), so withholding the client is a
   complete rollback for every client-side defect in this release.
2. **Migration `0011` is additive.** It adds `visible_on_board boolean not
   null default true` and append-only `create or replace` re-issues of
   `link_game`, `set_board_visibility`, `self_context`, `_hiscores_public`,
   `_profile_public` and `evolved_capabilities`. It drops nothing, rewrites no
   data, and does not re-issue the ownership guards.
3. **Its only dangerous direction is old client / new server.** Once `0011` is
   applied hosted but before any 3.0.0 client ships, an old client generates
   its local uuid, links, receives the server-generated uuid, discards it, and
   every later submit raises `game_mismatch`/42501
   (`docs/account-repair/ACCOUNT-IDENTITY-REWORK.md:63-71`).
4. **Therefore: if release verification fails *after* `0011` is applied
   hosted, hold the client — do not revert the migration.** Keep `0011`
   applied and keep the existing 2.0.2 path working; stop the 3.0.0 client
   instead. Reverting the migration is not the mitigation for a failed client
   verification.
5. **Accepted residual, stated plainly.** Because `0011` and the client are
   coupled they are applied and shipped in one window, so holding the client
   after `0011` is applied leaves the old-client/new-server window in (3) open
   for as long as the hold lasts. That exposure is accepted rather than fixed
   — it is the reason the hold should be short and the window deliberate.

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
