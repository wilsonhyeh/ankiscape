# AnkiScape: Evolved 3.0 — AnkiWeb listing draft (Wilson pastes manually; no API)

Title: AnkiScape: Evolved — OSRS Skills for Anki

> Turn reviews into skill grinding. Six OSRS-style skills, a Bank,
> achievements, hiscores, and optional free account sync across desktops.
> Your old Classic progress stays exactly as it was.

## What it does

- Six skills: Mining, Woodcutting, Smithing, Crafting, Fishing, Cooking.
  Every correct review earns XP — including failed gathers and burns.
- One nonmodal game window with an icon rail — Training, Skills, Bank,
  Achievements, Hiscores, Settings — that can stay open beside Anki and
  live-updates while you review.
- Train commits a skill + resource; every tier stays visible, locked tiers
  show the level they need, and Train (starts paused) labels a recipe whose
  materials are missing.
- A compact review HUD anchored above or below the card area (never over
  your card) with the active skill, level, XP bar, and pause reasons.
- Read-only Bank, achievements, and per-skill hiscores inside the add-on.
- Phone reviews (AnkiMobile/AnkiDroid) earn catch-up rewards after syncing
  to desktop. No phone game UI.
- Optional free account: syncs one game across your desktops. Offline play
  needs no account; everything uploads later, including pre-account progress.

## First run

New installs walk through a short guided setup: pick a gathering skill,
confirm a starting resource, then start studying — Anki stays the study app
throughout. Existing Classic players choose Try Evolved or Continue Classic;
the choice applies immediately (no restart), both games keep their own
progress, and you can switch any time from Settings -> Advanced or the deck
list (leave the reviewer first). Evolved starts fresh at level 1/0 XP —
Classic progress does NOT transfer (hiscore fairness), and Classic stays
frozen.

## Honest notes

- Undo retracts its review's rewards (Redo restores); scores and levels can
  decrease after Undo or sync reconciliation.
- Production training pauses automatically when materials run out or Undo
  drops the level below the recipe: reviews still count for Anki, earn no
  XP/items, and show exactly what is missing. Nothing switches back
  automatically — gathering never silently restarts production.
- Offline results are provisional until reconciliation. If two desktops
  consume the same last ingredient, the losing action earns zero (the old
  2.x practice-XP behavior is kept only for historical operations).
- Login is forgotten on restart (deliberate). Your game stays put.
- Sync runs on Supabase Free (pauses when idle; finite storage). At
  capacity the add-on keeps everything locally and retries later.
- Sound is off by default; celebrations and motion can be reduced in
  Settings -> Appearance.
- Privacy: uploads are opaque review identities + skill selections — never
  card text, decks, or collection contents. Email is verification/recovery
  only, never shown.

## Requirements

Anki 23.10+, any OS (macOS/Linux/Windows), Qt5/Qt6.
