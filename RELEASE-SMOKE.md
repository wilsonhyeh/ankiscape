# RELEASE-SMOKE.md - Real-Anki verification (AnkiScape 3.0)

HQ's generic `tauri dev` wording does not apply to this Python add-on. Use
this checklist with real Anki only. Synthetic revlog fixtures test
reconciliation; they do not prove AnkiMobile/AnkiDroid interop - that needs
the Wilson-operated device checklist at the bottom.

## Preconditions

- Artifact built by `python3 scripts/build_addon.py` (record SHA-256 +
  `prod_endpoint_baked` flavor from `dist/manifest.json`).
- Isolated base: `python3 dev.py launch --scenario <name>` (never personal).
- Real-user journeys on the packaged prod artifact: double-click
  `Launch AnkiScape.command` (or `dev.py launch --scenario
  user-new|user-upgrade|user-resume`) — shared isolated `.dev/anki/user`
  profile, production backend, artifact rebuilt automatically when shipped
  source changed, hash printed at launch.
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
6. Enable Keep me signed in -> restart with Resume -> account restored from
   Keychain. Log out -> restart -> signed out. Unchecked remember stays
   session-only. Offline review -> restart -> reconnect -> pending uploads in bounded
   batches, newer reviews never cleared on acknowledgment.
7. Login -> Forgot password -> request code -> enter code/new password.
   A bad code or rejected password keeps the form open for correction.
   `dev/account_regressions.py` verifies hosted signup/login/refresh/recovery
   with admin-generated codes (no email sent), then deletes its test account.
   Inbox delivery remains a manual check.
8. Undo/re-answer policy; Anki scheduling/Undo still works; Redo restores.
9. Corrupt/unsupported state recovery via Export/Restore Game Backup.
10. UI/UX shell journeys: `ui-onboarding`, `ui-training`, `ui-settings`,
    `ui-review` (anchored HUD, pause state, completion recap),
    `ui-lifecycle` (singleton window, reviewer switch rejection, immediate
    switch both ways).

Fail on native exception dialogs, hangs, missing screenshots/assertions, or a
dead local service.

## Earlier release-candidate UI/UX verification (before user-reported fixes)

Artifact: `ankiscape-3.0.0.ankiaddon`, sha256 `5c7575ac85652f0b...`, 155
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

### Hosted deployment (PERFORMED 2026-09-10)

- Pre-migration backup: schema + data dumps taken to `/tmp/ankiscape-deploy/`
  before applying.
- `supabase db push` applied only `0004_authoritative_scoring.sql` to the
  hosted `ankiscape` project (`vjqzamuogcughdvzskmf`); `supabase migration
  list` shows 0004 on both sides.
- Hosted capabilities now answer `protocol_version: 2`,
  `authoritative_scoring: true`; `hiscores` still serves anonymously.
- `dev/prod_e2e.py` against the hosted project: ALL PASS (22 checks) on the
  real stack, including authoritative XP written from stored operations,
  retraction reversing it, exact-retry stability, id-conflict reporting,
  lost-ack convergence, username login +/- , and the old-client gate
  (`client_update_required` for a submission without the protocol header).
  Test users/games cleaned up by the script.
- Net effect: hosted scoring is live. An older add-on build without the
  protocol header can no longer submit; it reports `Server update required`
  and keeps local pending progress until it updates. The new artifact
  (`5c7575ac...`) sends the header and is the compatible client.

### Hosted migration 0005 (PERFORMED 2026-09-10)

- Pre-migration backup: schema + data dumps to
  `/tmp/ankiscape-deploy-0005/` before applying.
- `supabase db push --dry-run` listed only `0005_gem_inventory_grant.sql`;
  the push applied it; `supabase migration list` shows 0005 on both sides.
- Post-deploy schema dump confirms both corrected functions: `gem_out` is
  written to the inventory in `evolved_replay`, and `_evolved_apply_direct`
  gates cooking by `cooking_level`.
- `dev/prod_e2e.py` against the hosted project: ALL PASS (28 checks),
  including two new checks that submit a deterministic gem-drop mining op and
  read the replayed server state: "mined gem granted to Bank" and "gem bonus
  XP applied (57.75)". Test users/games cleaned up.
- Net effect: gem-based Crafting (tiers 11-23) now scores server-side, and
  below-level cooking pauses in authoritative replay, matching the fixed
  client (artifact `cbd4516a`).

### Remaining manual release steps

- Wilson's real-inbox delivery smoke (register with a real email, OTP from
  `ankiscape@ankiscape.xyz`) — pre-confirmed test users skip delivery, so
  Resend-to-inbox remains unproven by design.
- AnkiWeb upload of `dist/ankiscape-3.0.0.ankiaddon` (no API; manual).
- Tag / GitHub Release publication (no workflow publishes automatically).
- Mobile (AnkiMobile/AnkiDroid) checklist below, and Linux/Windows/Qt5
  platform evidence — deferred, not claimed.

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

## User-reported repair pass (unpublished, 2026-09-10)

See `artifacts/user-reported-fixes-2026-09-10.md`: 219 Python tests, 11/11
scenarios on both macOS Anki versions, plus hosted account/recovery and
Keychain checks. These local changes supersede the earlier frozen RC for
launcher testing; they are not a new published release. Real inbox delivery
and Windows/Linux GUI verification remain manual.

## Reliability and support workstream (unreleased, 2026-09-10)

Local implementation for the reliability/support plan is recorded in
`docs/RELIABILITY.md`, `docs/TEST-MATRIX.md` and
`artifacts/reliability/baseline/`. Status at handoff:

- Engine: accepted answers persist atomically (25 ms interactive budget) and
  the projection worker publishes off-thread; release-profile budgets pass
  locally (hook p95 <=0.35 ms, reward completion ~113 ms, 100k rebuild
  ~5.5 s, state equivalence true).
- Evidence: `dev/reliability.py` validates provenance/scenarios/budgets;
  `dev/matrix.json`/`dev/reliability-matrix.json` still require all seven
  targets; local `verify --stage pr` fails naming the CI workflow (expected).
- Platforms: seven-target lanes are prepared but **not run** — pinned runtime
  downloads fail closed until `dev/runtime_manifest.json` records verified
  URLs and SHA-256 pins, and `.github/workflows/*` are not pushed.
- Migration `0006_test_cohorts.sql` and `0004_test_cohorts.test.sql` are
  prepared, **not deployed**; hosted fixture seeding is **blocked** until an
  authorized execution session supplies credentials and applies it.
- Support: Report a bug dialog and GitHub templates/configuration script are
  in place; hosted label/issue-form/vulnerability-reporting changes are
  **blocked** on `scripts/configure_github.py --apply` authorization.
- Still manual and unverified: Wilson's real-inbox delivery, AnkiMobile/
  AnkiDroid sync, and artwork redistribution permission (see
  `docs/ASSET-RIGHTS.md`). No agent sends email; automated auth codes do not
  prove real-inbox delivery.

Do not call the release ready while any required platform/scenario/manual
prerequisite above remains missing.
