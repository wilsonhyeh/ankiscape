# AnkiScape: Evolved (3.0)

AnkiScape layers OSRS-style skill grinding onto Anki card reviews: correct
answers earn XP in six skills (Mining, Woodcutting, Smithing, Crafting,
Fishing, Cooking), with items, achievements, level-ups, and an XP HUD.
Classic mode (2.0.2 gameplay) stays frozen and switchable; Evolved starts
fresh at level 1/0 XP (no transfer — hiscore fairness).

Same AnkiWeb listing `1808450369`, single 3.0.0 release. Anki 23.10+,
Qt5/Qt6, macOS/Linux/Windows.

## Install

1. Download `ankiscape-3.0.0.ankiaddon` from the release.
2. In Anki: Tools -> Add-ons -> Install from file -> select the file.
3. Restart Anki. New installs enter guided Evolved setup (pick a gathering
   skill and starting resource). Existing Classic users get an explicit
   Try Evolved / Continue Classic choice; nothing restarts, and both games
   keep their own progress. Switch modes any time from the deck list or
   Settings -> Advanced (leave the reviewer first).

## Offline / sync / recovery

- Offline single-desktop play needs no account. Everything is stored in a
  local journal next to your Anki profile (never in Anki's own tables).
- Phone reviews (AnkiMobile/AnkiDroid) earn catch-up rewards after their
  history syncs to desktop, under your desktop gathering preset
  (Mining/Woodcutting/Fishing, default Mining; Settings -> preset).
- A free account syncs one Evolved game across desktops (one account links
  to one game; the link is immutable in 3.0 and synced copies share it).
  Progress uploads within about ten seconds of a new change while online;
  play before registration, while logged out, or offline uploads later.
  Offline results are provisional until reconciliation; concurrent
  ingredient conflicts resolve to zero reward for the losing action (policy
  2), and reconciliation says so.
- Keep me signed in stores the session in macOS Keychain (or an available system credential backend). Passwords are never saved; Log out removes the saved session. If no secure backend is available, sign-in lasts for this session only.
- Forgot password is available from Log in and Settings → Account. Request an email code, then enter it with a new password.
- The Guide tab is an offline handbook covering all six skills, exact XP formulas, every recipe and resource, levels 1–99, gems, achievements, catch-up, accounts, and backups. Tables are generated from the shipped game rules.
- Undo retracts its review's rewards; Redo restores them; a replacement
  answer earns once. Scores and levels can decrease after Undo/sync.
- Production training pauses automatically when materials run out or Undo
  drops the level below the recipe: reviews still count for Anki, earn no
  XP/items, and show the exact missing ingredients. Resumes on the next
  eligible review once materials return; switching to gathering never
  switches back automatically.
- Settings -> Export/Restore Game Backup covers corrupt journals. An
  anonymous game NEEDS its backup file; a linked account can also rebuild
  from the server plus its preserved outbox backup.

## Interface

- One nonmodal game window with an icon rail: Training, Skills, Bank,
  Achievements, Hiscores, plus a Settings cog. It can stay open beside Anki
  and live-updates during reviews.
- Training Home leads with your current skill/resource, level progress,
  reward expectations, material readiness, and a Change training action.
- Skills shows every resource tier (locked tiers stay visible with required
  levels); Train commits the skill + resource, and Train (starts paused)
  labels a recipe whose materials are missing.
- The review HUD is anchored in reserved space above or below the card
  area (default below) — never floating over card content. Reward flashes
  are brief; level-ups/unlocks get a short celebration. Sound is off by
  default.
- Settings is grouped: Appearance & HUD (UI scale, HUD position,
  celebrations, reduced motion, sound), Training & Catch-up, Account &
  Sync, and Advanced (backups, diagnostics, mode switch).

## Privacy

- Reviews upload as opaque review identities + skill/resource selections —
  never card text, deck names, note fields, raw card IDs, or collection
  contents.
- Email is required for verification/recovery and is never shown on
  hiscores. Tokens/passwords never enter collection config, journals, or
  logs.
- Hiscores (in-add-on, per-skill) are browsable without an account: username
  + XP only. Signing in adds your own score and live sync. Five permanent
  sample players appear labeled "Demo". No friends, clans or public site.
- Free-backend note: the sync backend runs on Supabase Free (pauses after
  a week idle; finite storage). At capacity the add-on stops online writes
  safely and keeps local pending operations — no paid upgrade, no
  keep-alive evasion.

## Compatibility

- Anki 23.10+ (Qt5/Qt6 via `aqt.qt`); verified on 26.08.1 + 23.10.1 Qt6 macOS.
- Network: stdlib only, 10 s timeouts, background threads. Sync triggers:
  login, Anki-sync finish, 200 new reviews, 20 min activity, manual Sync.
- Classic mode frozen (routing/lifecycle/switching only).

## Settings overview

All user-visible options are in Anki: Tools → Add-ons → AnkiScape → Config/Settings.

- Experience
  - Enable experience HUD: toggles the entire in-review HUD (icon + level + progress).
- Notifications
  - Enable floating XP: toggles the small XP toast when XP is earned.
  - Enable achievements and level up pop ups: toggles achievement/level-up dialogs.
- Floating widget
  - Enable widget: show/hide the small floating button.
  - Widget Position: left/right.

### Defaults
- Enable experience HUD: true
- Enable floating XP: true
- Enable achievements and level up pop ups: true
- Enable widget: true
- Widget Position: "right"

### Migration of legacy settings
If you previously used a separate “progress bar” toggle, it’s automatically migrated on profile load:
- `ankiscape_hud_progress_enabled` → `ankiscape_review_hud_enabled` (only if the new key wasn’t set).
This migration is idempotent and covered by tests.

## Development

### Run tests
Use the helper script at the repo root:

```
python3 run_tests.py
```

- Uses Python’s built-in `unittest`.
- Discovers `tests/test_*.py`.
- Includes an integration smoke test that loads the add-on without Qt and verifies hook flow + settings gating.

### Debug logging
Set an environment variable before launching Anki or running tests to enable debug logs:
- macOS/Linux (zsh): `export ANKISCAPE_DEBUG=1`
- Windows (PowerShell): `$env:ANKISCAPE_DEBUG = '1'`

Logs are written next to the package as `ankiscape_debug.log` and rotate automatically. They’re git-ignored.

## Development

### Dev playground
```
python3 dev.py setup
python3 dev.py launch --scenario fresh        # chooser + 20 cards
python3 dev.py test --suite e2e --journey sync     # live local-stack sync
python3 dev.py test --suite e2e --journey dialogs  # live Qt menu + dialogs
python3 dev.py verify --release
```
Synthetic CLI tests keep state under gitignored `.dev/` and reports under
`artifacts/`; they do not touch personal collections or production.
Double-click `Launch AnkiScape.command` for production rehearsal in an isolated
test profile. It uses the live backend and defaults to Resume when a profile
exists. New user and Upgrade replace that test profile; Resume preserves it.

## Packaging notes
Deterministic build from an explicit allowlist:
```
python3 scripts/build_addon.py   # -> dist/ankiscape-3.0.0.ankiaddon + manifest
```
Excludes tests/dev/server/artifacts/dist/.dev/.git/.venv, credentials, journals,
logs, caches, editor files. Secret-content scan fails the build on privileged
patterns (public anon keys are distinguished). A code/rules/asset change after
packaging invalidates that artifact's tests — rebuild and reverify.
Production endpoint is baked per release with `scripts/make_prod_config.py`
(public anon key only; never service-role/SMTP keys).
