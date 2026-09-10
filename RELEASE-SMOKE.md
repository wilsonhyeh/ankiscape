# RELEASE-SMOKE.md - Real-Anki verification (AnkiScape 3.0)

HQ's generic `tauri dev` wording does not apply to this Python add-on. Use
this checklist with real Anki only. Synthetic revlog fixtures test
reconciliation; they do not prove AnkiMobile/AnkiDroid interop - that needs
the Wilson-operated device checklist at the bottom.

## Preconditions

- Artifact built by `python3 scripts/build_addon.py` (record SHA-256 +
  `prod_endpoint_baked` flavor from `dist/manifest.json`).
- Isolated base: `python3 dev.py launch --scenario <name>` (never personal).
- Local Supabase stack healthy (`cd server && supabase status`).
- Dev builds (no prod endpoint baked) point at the local stack only and
  refuse prod-looking HTTPS; prod builds carry the baked anon endpoint.

## Journeys (all on the packaged `.ankiaddon`, never a source symlink)

1. Fresh install -> guided Evolved setup (Welcome -> gathering skill ->
   level-1 resource -> how rewards work -> Start studying) -> Mining ->
   reveal/answer a real card -> XP/item/HUD update -> close/relaunch ->
   setup resumes where it stopped until committed; after commit, progress
   is preserved.
2. Earn starter ingredients through real review -> Smithing/Crafting/Fishing/
   Cooking -> success, failed gather, burn, depleted materials -> expected
   XP/consumption/counters; a policy-2 production review with missing
   materials persists, earns zero, and shows the pause reason.
3. Install 2.0.2 fixture -> package upgrade -> Try Evolved / Continue
   Classic prompt -> Continue Classic keeps Classic; Try Evolved opens
   setup in the same visit (no restart) -> later Settings -> Advanced ->
   Switch to Classic switches immediately, preserving both games.
4. Two profiles/modes in one process; close a profile with a request
   outstanding; no stale callback, duplicate hook, wrong-account write,
   orphaned HUD, or timer.
5. Register/login (local Auth) -> answer cards -> batch upload ->
   leaderboard/profile agree -> retry after lost response -> totals unchanged.
   Automated: `python3 dev.py test --suite e2e --journey sync` (real local
   stack: two users, offline credit, second-device merge, id-conflict,
   lost-ack retry).
5b. Shell + account screens live: `python3 dev.py test --suite e2e --journey
   dialogs` (icon rail + Hiscores controls + register/login/recovery submit
   paths through the shipped builders).
6. Offline review -> restart -> reconnect/login -> pending uploads in bounded
   batches, newer reviews never cleared on acknowledgment.
7. Email-code recovery via captured local mail; old password/token fails.
8. Undo/re-answer policy; Anki scheduling/Undo still works; Redo restores.
9. Corrupt/unsupported state recovery via Export/Restore Game Backup.
10. UI/UX shell journeys: `ui-onboarding`, `ui-training`, `ui-settings`,
    `ui-review` (anchored HUD, pause state, completion recap),
    `ui-lifecycle` (singleton window, reviewer switch rejection, immediate
    switch both ways).

Fail on native exception dialogs, hangs, missing screenshots/assertions, or a
dead local service.

## UI/UX verification (2026-09-10, macOS, packaged artifact)

Artifact: `ankiscape-3.0.0.ankiaddon`, sha256 `3cde9bc657675eed...`, 155
members, prod endpoint baked. Commands and results:

- `python3 dev/ui_ux_verify.py --anki 26.08.1 --qt 6` -> PASS (11/11
  scenarios, 96 screenshots, Qt 6.11.0)
- `python3 dev/ui_ux_verify.py --anki 23.10 --qt 6` -> PASS (11/11
  scenarios, Qt 6.5.3)
- `python3 dev/scoring_parity.py --local` -> PASS (Python == PostgreSQL:
  200-op batch, late arrival, duplicate delivery, material conflict,
  undo/restore, mixed policy, plus the REST API case)
- `python3 dev.py verify --release` -> PASS (214 Python tests, package,
  backend reset x2 + pgTAP, live Auth/RPC smoke, fresh E2E)

Report with inspected findings: `artifacts/ui-ux/report.md`; screenshots per
scenario under `artifacts/ui-ux/<anki>-qt<qt>-<stamp>/`.

### Hosted step NOT performed

Server-authoritative scoring ships as local migration
`0004_authoritative_scoring.sql`. Deploying it to the hosted Supabase project
(`supabase db push` for the `ankiscape` project, then smoke) is intentionally
NOT done here and requires separate authorization. Until then a hosted
submit rejects new-protocol payloads with `client_update_required` /
`Server update required`, local pending progress is preserved, and no hosted
scoring claim is made.

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
