# Release readiness — AnkiScape 3.0.0 (Evolved)

Status: **BLOCKED — release rehearsal not complete.**

Frozen source SHA: _not yet frozen_
Artifact: _not yet frozen_ (name `ankiscape-3.0.0.ankiaddon`)
Artifact SHA-256: _pending_
Baked project: expected `vjqzamuogcughdvzskmf` (Supabase `ankiscape`)
AnkiWeb ID: `1808450369` (same listing; verify current published version
immediately before publication)
Evidence run: _pending release-verify run id/URL_

## Requirements matrix

| Requirement | Status | Evidence |
|---|---|---|
| Seven native targets (macOS/Windows/Linux × oldest/current, Qt5+Qt6) | pending | release-verify `lanes` |
| Native performance + two-hour endurance per target | pending | `native-performance` / `endurance-2h` metrics |
| Shared checks + engine benchmarks | pending | `shared` record |
| Linux backend: suite, parity, account contracts, sync, full mutation | pending | `backend` record |
| Public demo board: five labeled demos, retried 24-account suite retired | pending | `hosted` record + `dev/demo_players.py --verify` |
| Account identity: two-game model (local vs account), server-owned game uuid, login adoption, retired local outbox never uploaded, durable download-first sync, ten-second scheduling | pending | ui-account-lifecycle lane records + dev/account_journey_e2e.py + tests/test_bricked_recovery.py |
| Hosted migrations 0008/0009 and `account-status` deploy | blocked — authorization | repair-plan hosted runbook |
| Hosted native public browsing/labels per current OS | pending | `native-journeys` target requirements |
| Real-inbox delivery (human) | blocked | Wilson |
| AnkiMobile/AnkiDroid checklist (human) | blocked | Wilson |
| Artwork publication decision (owner) | blocked | Wilson / ASSET-RIGHTS |
| Rollback note and backup restore rehearsal | prepared | `ROLLBACK.md` |

Publication requires: every automated row green on one immutable candidate,
human rows recorded, and Wilson's explicit authorization. Do not treat this
file as a go decision.
