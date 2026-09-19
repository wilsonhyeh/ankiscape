# AnkiScape 3.0.0 — account/identity rework (two-game model)

Implementation record for the 3.0.0 account/identity rework. This file is the
client-side companion to the revision-2/revision-3 design deltas under
`.opencode/workflow-reports/`; it records what shipped, where, and the
residual risk that was accepted rather than silently dropped.

## Decisions (settled)

- **D1** — Each profile has at most two Evolved games: an offline (local-only)
  game that never uploads, and an account game (server-owned uuid) that
  uploads. One active-game pointer selects which is live.
- **D2** — Attribution follows the ACTIVE GAME, never the login state. The
  offline game never uploads while signed in; the account game never imports
  the offline game's history.
- **D3** — Login swaps the active pointer to the account game; logout swaps it
  back to the offline game.
- **D4** — Nothing is imported or deleted on link. The account game starts at
  level 1 / 0 XP, stated on the register notice before the user commits.
- **D5** — `link_game` returns the account's game, creating one when absent,
  and never raises `game_claimed`/`game_mismatch`.
- **D6** — First run offers Create account (primary) or Play offline. No game
  is minted before the user picks a path.
- **D7** — `players.visible_on_board` is enforced server-side and filters the
  public board only (`hiscores`/`public_profile`), never the test-cohort
  RPCs. The toggle is disclosed at signup.

## Client implementation anchors

| Area | Where |
|---|---|
| Link reply (`created` key rule, uuid adoption) | `evolved/link.py` (`LinkResult`, `link_reply_adoptable`, `ensure_link`) |
| Two-game slots, offer rule, notice predicate | `evolved/onboarding.py` (`link_slots`, `coordinator_offer_uuid`, `register_notice_variant`, `active_local_game`) |
| Post-auth coordinator (link → materialize → slots → drain → swap → bind → sync) | `__init__.py` (`_evolved_post_auth_coordinator`, `_evolved_adopt_account_game`, `_evolved_swap_active_game`, `_evolved_bootstrap_download`) |
| Sync gated to the account game | `__init__.py` (`_evolved_sync_service`) |
| Outbox retirement (unbound journals only) | `evolved/journal.py` (`retire_outbox`) |
| Relogin recovery state and copy | `evolved/service.py` (`state_token`), `evolved/ui/shell.py` (`status_text`) |
| Capability/self_context plumbing | `evolved/service.py` (`fetch_capabilities`, `set_board_visibility`), `__init__.py` (`get_evolved_capabilities`, `get_self_context`, `on_board_visibility`), `evolved/ui/settings.py` (`board_visibility_row`) |
| Register notice (both variants) | `evolved/ui/account.py` (`REGISTER_NOTICE_*`, `register_notice_text`) |

## Residual risk (accepted, disclosed)

Retained local operations — kept by design with `acked = 0`, never deleted —
remain one `p_game_uuid` argument away from the public board. No AnkiScape
code path ever uploads them, but the server's only identity check on
`submit_operations` is `players.game_uuid = p_game_uuid`
(`server/supabase/migrations/0004_authoritative_scoring.sql:894-897`), and
the wire batch carries no game identity of its own. A caller that already
knows its own `game_uuid` could present it. 3.0.0 is unpublished and the
server cannot verify offline reviews regardless (the operations are
client-asserted facts by design), so this is accepted and disclosed rather
than patched client-side; the negative pgTAP case in
`server/supabase/tests/0007_account_identity.test.sql` pins the existing
guard as the containment.

## Mixed-version window (direction)

The dangerous window is **old client / new server**: once 0011 is applied
hosted but before any 3.0.0 client ships, an old client generates its local
uuid, links, receives the server-generated uuid, discards it, and every later
submit raises `game_mismatch`/42501. Acceptable only because 3.0.0 is
unpublished and the hosted migration ships in the same release window as the
client. Requiring the `created` key protects the opposite window (new client /
old server) and does not mitigate this one.

## Fixture hygiene

No test, scenario, or fixture reads `artifacts/account-repair/private/**`.
The bricked-recovery case (`tests/test_bricked_recovery.py`) builds its
synthetic unbound profile from scratch through the Journal API.
