# RELEASE-SMOKE.md - Real-Anki verification (AnkiScape 3.0)

HQ's generic `tauri dev` wording does not apply to this Python add-on. Use
this checklist with real Anki only. Synthetic revlog fixtures test
reconciliation; they do not prove AnkiMobile/AnkiDroid interop - that needs
the Wilson-operated device checklist at the bottom.

## Preconditions

- Artifact built by `python3 scripts/build_addon.py` (record SHA-256).
- Isolated base: `python3 dev.py launch --scenario <name>` (never personal).
- Local Supabase stack healthy (`cd server && supabase status`).

## Journeys (all on the packaged `.ankiaddon`, never a source symlink)

1. Fresh install -> chooser (Evolved preselected) -> Evolved -> Mining ->
   reveal/answer a real card -> XP/item/HUD update -> close/relaunch ->
   progress preserved.
2. Earn starter ingredients through real review -> Smithing/Crafting/Fishing/
   Cooking -> success, failed gather, burn, depleted materials -> expected
   XP/consumption/counters.
3. Install 2.0.2 fixture -> package upgrade -> Classic -> verify original
   state/behavior -> switch/restart into Evolved -> fresh progress -> return
   to Classic unchanged.
4. Two profiles/modes in one process; close a profile with a request
   outstanding; no stale callback, duplicate hook, wrong-account write,
   orphaned HUD, or timer.
5. Register/login (local Auth) -> answer cards -> batch upload ->
   leaderboard/profile agree -> retry after lost response -> totals unchanged.
6. Offline review -> restart -> reconnect/login -> pending uploads in bounded
   batches, newer reviews never cleared on acknowledgment.
7. Email-code recovery via captured local mail; old password/token fails.
8. Undo/re-answer policy; Anki scheduling/Undo still works.
9. Corrupt/unsupported state recovery via Export/Restore Game Backup.

Fail on native exception dialogs, hangs, missing screenshots/assertions, or a
dead local service.

## Wilson-operated mobile sync checklist (before claiming interop)

- [ ] iOS: review on AnkiMobile -> sync -> desktop import -> catch-up reward
        under the desktop gathering preset, exactly once.
- [ ] Android: same via AnkiDroid.
- [ ] Mobile Undo before first ingestion leaves no eligible record (observed,
        not inferred).
- [ ] Phone deletion of an imported review reconciles only when the deletion
        is observable in synced history.

## Post-upload smoke (separately authorized stage only)

Updater behavior, exact package identity (`1808450369`), Classic preservation,
production gameplay/account flow. Complements - never replaces - pre-upload tests.
